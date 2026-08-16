"use client";

import { Board } from "../components/Board";
import { Chat } from "../components/Chat";
import { useBoard } from "../lib/useBoard";

export default function HomePage() {
  const board = useBoard();

  return (
    <main>
      <header className="page-header">
        <div>
          <p className="page-header__eyebrow">Trellis / authoritative state</p>
          <h1>Committed work</h1>
        </div>
        <p className="page-header__note">
          This board reads task state from FastAPI. Refreshing replaces the view
          with the latest committed database state.
        </p>
      </header>
      <Chat onRunComplete={board.refetch} />
      <Board state={board} />
    </main>
  );
}
