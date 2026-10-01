"""scripts/start_backend.sh decision logic, run against stubs: a copy of the script in a temp repo with a fake .env and
stub docker / uv / lsof first on PATH. Nothing real is started, seeded, loaded or written outside tmp_path."""
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "start_backend.sh"

UV_STUB = r"""#!/usr/bin/env bash
echo "uv $*" >> "$STUB_LOG"
case "$*" in
  sync*) exit 0 ;;
  *"prism.sim.cli --check"*) exit "${SEED_RC:-0}" ;;
  *"prism.sim.cli"*) exit 0 ;;
  *"prism.graph.cli ping"*)
    n=$(cat "$STUB_DIR/ping_count" 2>/dev/null || echo 0); echo $((n + 1)) > "$STUB_DIR/ping_count"
    echo "ping: stub says ${PING_MSG:-AuthError}" >&2
    exit "${PING_RC:-0}" ;;
  *"prism.graph.cli check"*) exit "${CHECK_RC:-0}" ;;
  *"prism.graph.cli load"*) touch "$STUB_DIR/loaded"; exit 0 ;;
  *"prism.graph.embedder download"*)
    touch "$STUB_DIR/downloaded"
    mkdir -p .models/snap && printf 'onnx' > .models/snap/model_optimized.onnx
    exit 0 ;;
  *honcho*) echo "honcho env PRISM_GATEWAY_PORT=${PRISM_GATEWAY_PORT:-unset} AGENT_PORT=${PRISM_AGENT_PORT:-unset} DEV_TOKEN=${PRISM_AGENT_DEV_TOKEN_ENABLED:-unset}" >> "$STUB_LOG"; exit 0 ;;
esac
exit 0
"""


def _stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / "start_backend.sh")
    (root / ".env.example").write_text("PRISM_ENV=dev\n")
    (root / "backend" / ".models" / "snap").mkdir(parents=True)
    (root / "backend" / ".models" / "snap" / "model_optimized.onnx").write_text("onnx")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _stub(stubs / "uv", UV_STUB)
    _stub(stubs / "docker", "#!/usr/bin/env bash\necho \"docker $*\" >> \"$STUB_LOG\"\nexit 0\n")
    _stub(stubs / "lsof", "#!/usr/bin/env bash\necho \"lsof $*\" >> \"$STUB_LOG\"\nexit 1\n")
    _stub(stubs / "sleep", "#!/usr/bin/env bash\nexit 0\n")   # the bolt poll's sleep: instant
    return root, stubs


def run(repo, *, env_file: str | None = "", timeout=60, **env) -> subprocess.CompletedProcess:
    root, stubs = repo
    if env_file is not None:
        (root / ".env").write_text(env_file)
    log = stubs / "log"
    base = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(root), "STUB_LOG": str(log), "STUB_DIR": str(stubs)}
    proc = subprocess.run(["bash", str(root / "scripts" / "start_backend.sh")], cwd=root, capture_output=True,
                          text=True, timeout=timeout, env={**base, **{k: str(v) for k, v in env.items()}})
    proc.log = log.read_text() if log.exists() else ""  # type: ignore[attr-defined]
    return proc


def test_the_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


@pytest.mark.parametrize("rc,loads,ok", [(0, False, True), (1, True, True), (2, False, False), (3, False, False)])
def test_graph_is_loaded_only_when_check_says_empty_or_stale(repo, rc, loads, ok):
    proc = run(repo, CHECK_RC=rc)
    assert (proc.returncode == 0) is ok, proc.stderr
    assert (repo[1] / "loaded").exists() is loads
    if not ok:
        assert "could not determine the context graph state" in proc.stderr
        assert "honcho" not in proc.log


def test_an_undetermined_seed_check_never_reseeds(repo):
    proc = run(repo, SEED_RC=2)
    assert proc.returncode != 0 and "--reset" not in proc.log


SECRETS = ("PRISM_CTX_HMAC_KEY", "PRISM_JWT_SECRET", "PRISM_AUDIT_HMAC_KEY")


def _values(env_text: str, name: str) -> list[str]:
    out = []
    for line in env_text.splitlines():
        line = line.removeprefix("export ").strip()
        if line.startswith(f"{name}="):
            out.append(line.split("=", 1)[1].strip().strip("'\""))
    return out


@pytest.mark.parametrize("line", ["{n}=", '{n}=""', "{n}=''", "export {n}=", 'export {n}=""', "{n}=  "])
def test_ensure_secret_fills_empty_values(repo, line):
    env = "".join(line.format(n=n) + "\n" for n in SECRETS) + "OTHER=keep\n"
    proc = run(repo, env_file=env)
    assert proc.returncode == 0, proc.stderr
    text = (repo[0] / ".env").read_text()
    for name in SECRETS:
        (value,) = _values(text, name)   # filled in place: one line, never a second one appended
        assert len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    assert "OTHER=keep" in text and "Generated PRISM_JWT_SECRET" in proc.stdout


@pytest.mark.parametrize("line", ["{n}={v}", 'export {n}="{v}"', "{n}='{v}'"])
def test_ensure_secret_keeps_set_values(repo, line):
    env = "".join(line.format(n=n, v=f"{n.lower()}-kept-0123456789abcdef0123") + "\n" for n in SECRETS)
    proc = run(repo, env_file=env)
    assert proc.returncode == 0, proc.stderr
    assert (repo[0] / ".env").read_text() == env and "Generated" not in proc.stdout


def test_missing_secrets_are_appended(repo):
    proc = run(repo, env_file="PRISM_ENV=dev")   # no trailing newline
    text = (repo[0] / ".env").read_text()
    assert proc.returncode == 0 and text.startswith("PRISM_ENV=dev\n")
    assert all(len(_values(text, n)) == 1 for n in SECRETS)


def test_bolt_poll_timeout_shows_the_ping_error_once(repo):
    proc = run(repo, PING_RC=2, PRISM_BOLT_WAIT_S=1)
    assert proc.returncode != 0
    assert "did not answer on bolt" in proc.stderr
    assert proc.stderr.count("ping: stub says AuthError") == 1   # the polls are quiet; the last attempt is shown
    assert "honcho" not in proc.log


def test_an_incomplete_model_cache_is_downloaded_again(repo):
    root, stubs = repo
    (root / "backend" / ".models" / "snap" / "model_optimized.onnx").unlink()
    (root / "backend" / ".models" / "snap" / "config.json").write_text("{}")   # partial download: no model file
    proc = run(repo)
    assert proc.returncode == 0, proc.stderr
    assert (stubs / "downloaded").exists()


def test_a_complete_model_cache_is_not_downloaded(repo):
    proc = run(repo)
    assert proc.returncode == 0 and not (repo[1] / "downloaded").exists()


def test_gateway_port_comes_from_prism_gateway_port(repo):
    proc = run(repo, env_file="PRISM_GATEWAY_PORT=8299\n")
    assert proc.returncode == 0, proc.stderr
    assert "-iTCP:8299" in proc.log and "-iTCP:8200" not in proc.log
    assert "honcho env PRISM_GATEWAY_PORT=8299" in proc.log and "127.0.0.1:8299/mcp" in proc.stdout


def test_procfile_and_makefile_use_the_port_setting():
    root = SCRIPT.parents[1]
    assert "--port ${PRISM_GATEWAY_PORT:-8200}" in (root / "backend" / "Procfile").read_text()
    assert "--port $${PRISM_GATEWAY_PORT:-8200}" in (root / "Makefile").read_text()


@pytest.mark.parametrize("env_file,env,expected", [
    ("", {}, "true"),                                                  # default for the loopback launcher
    ("PRISM_AGENT_DEV_TOKEN_ENABLED=false\n", {}, "false"),            # .env is honoured
    ("export PRISM_AGENT_DEV_TOKEN_ENABLED='false'\n", {}, "false"),
    ("PRISM_AGENT_DEV_TOKEN_ENABLED=false\n", {"PRISM_AGENT_DEV_TOKEN_ENABLED": "true"}, "true"),   # env wins
    ("PRISM_AGENT_DEV_TOKEN_ENABLED=\n", {}, "true"),                  # empty counts as unset
    ("PRISM_AGENT_DEV_TOKEN_ENABLED=false   # off\n", {}, "false"),     # inline comment is stripped
    ("PRISM_AGENT_DEV_TOKEN_ENABLED=true # local demo only\n", {}, "true"),
    ("PRISM_AGENT_DEV_TOKEN_ENABLED=\"No\"\n", {}, "false"),
    ("PRISM_AGENT_DEV_TOKEN_ENABLED=1\n", {}, "true"),
    ("PRISM_AGENT_DEV_TOKEN_ENABLED=# only a comment\n", {}, "true"),
])
def test_dev_token_flag_precedence(repo, env_file, env, expected):
    proc = run(repo, env_file=env_file, **env)
    assert proc.returncode == 0, proc.stderr
    assert f"DEV_TOKEN={expected}" in proc.log


@pytest.mark.parametrize("agent,gateway,msg", [
    ("0", "8200", "between 1 and 65535"), ("70000", "8200", "between 1 and 65535"),
    ("8200", "8200", "must differ from PRISM_GATEWAY_PORT"), ("08200", "8200", "must differ"),
    ("abc", "8200", "must be a port number")])
def test_bad_agent_ports_are_rejected_before_anything_starts(repo, agent, gateway, msg):
    proc = run(repo, PRISM_AGENT_PORT=agent, PRISM_GATEWAY_PORT=gateway)
    assert proc.returncode != 0 and msg in proc.stderr and "docker compose" not in proc.log


def test_an_invalid_dev_token_flag_stops_the_launcher(repo):
    proc = run(repo, env_file="PRISM_AGENT_DEV_TOKEN_ENABLED=maybe\n")
    assert proc.returncode != 0 and "must be true or false" in proc.stderr and "docker compose" not in proc.log
