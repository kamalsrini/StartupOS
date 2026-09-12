import type { Table } from "@/lib/api";

// Cells arrive sanitized by the API: plain escaped text, or exactly one status pill / one link.
export default function DataTable({ table }: { table: Table | null | undefined }) {
  if (!table) return null;
  return (
    <div className="card">
      <div className="card-header">
        <div className="card-title">
          {table.title} {table.accent ? <span className="accent">/ {table.accent}</span> : null}
        </div>
      </div>
      <div style={{ overflowX: "auto" }}>
        <table className="fin-table">
          <thead>
            <tr>
              {table.cols.map((c, i) => (
                <th key={i}>{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {table.rows.length === 0 ? (
              <tr>
                <td colSpan={table.cols.length} className="fin-empty">
                  Nothing ingested yet.
                </td>
              </tr>
            ) : (
              table.rows.map((r, i) => (
                <tr key={i}>
                  {r.map((cell, j) => (
                    <td key={j} dangerouslySetInnerHTML={{ __html: cell }} />
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
