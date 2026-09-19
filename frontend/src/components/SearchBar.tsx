import { SearchIcon } from './Icons';

export function SearchBar({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <label className="panel search">
      <SearchIcon />
      <input
        id="search-input"
        className="f"
        type="text"
        placeholder="Search titles or genres"
        aria-label="Search titles or genres"
        autoComplete="off"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}
