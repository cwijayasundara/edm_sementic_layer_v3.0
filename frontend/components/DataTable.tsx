import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { formatCell } from "@/lib/format";

export function DataTable({ columns, rows }: { columns: string[]; rows: unknown[][] }) {
  const numeric = columns.map((_, j) => rows.length > 0 && rows.every((r) => r[j] === null || typeof r[j] === "number"));
  return (
    <div className="max-h-80 overflow-auto">
      <Table>
        <TableHeader>
          <TableRow className="hover:bg-transparent">{columns.map((c, j) => (
            <TableHead key={c} className={`${numeric[j] ? "text-right" : ""} sticky top-0 z-10 h-9 bg-[#f7f8fa] px-3 text-xs font-medium text-[var(--prism-muted)]`}>{c}</TableHead>))}</TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((r, i) => (
            <TableRow key={i}>{columns.map((c, j) => <TableCell key={c} className={`px-3 ${numeric[j] ? "text-right" : ""}`}>{formatCell(r[j])}</TableCell>)}</TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
