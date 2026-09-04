import type { Tile as TileT } from "@/lib/api";

export default function Tile({ tile }: { tile: TileT }) {
  return (
    <div className={`cash-tile ${tile.cls || ""}`}>
      <div className="cash-tile-label">{tile.label}</div>
      <div className="cash-tile-val">{tile.val}</div>
      <div className="cash-tile-sub">{tile.sub || " "}</div>
    </div>
  );
}
