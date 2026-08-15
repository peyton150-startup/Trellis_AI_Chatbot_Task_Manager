import { Board } from "../components/Board";

export default function HomePage() {
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
      <Board />
    </main>
  );
}
