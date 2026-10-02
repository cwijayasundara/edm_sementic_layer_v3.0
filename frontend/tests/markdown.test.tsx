import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Markdown } from "@/components/Markdown";

const html = (text: string) => render(<Markdown text={text} />).container.innerHTML;

describe("Markdown", () => {
  it("renders bold, italic and inline code", () => {
    const out = html("**Vendor A** has *most* `price_conflicts`.");
    expect(out).toContain("<strong>Vendor A</strong>");
    expect(out).toContain("<em>most</em>");
    expect(out).toContain("<code");
    expect(out).not.toContain("*");
  });

  it("splits paragraphs and renders bulleted and numbered lists", () => {
    const out = html("Intro line.\n\n- one\n- **two**\n\n1. first\n2. second");
    expect(out.match(/<p/g)).toHaveLength(1);
    expect(out).toMatch(/<ul[^>]*><li>one<\/li><li><strong>two<\/strong><\/li><\/ul>/);
    expect(out).toMatch(/<ol[^>]*><li>first<\/li><li>second<\/li><\/ol>/);
  });

  it("never renders HTML from the text", () => {
    const out = html("<img src=x onerror=alert(1)> **<b>x</b>**");
    expect(out).not.toContain("<img");
    expect(out).not.toContain("<b>");
    expect(out).toContain("&lt;img");
  });

  it("leaves an unmatched marker as text", () => {
    expect(html("2 * 3 = 6")).toContain("2 * 3 = 6");
  });
});
