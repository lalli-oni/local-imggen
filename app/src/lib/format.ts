// Small display helpers shared by the pages.

/** "2026-11-18T17:13:21" or "2026-11-18 06:03:12" -> "18 Nov 2026, 17:13" in the viewer's locale. */
export function when(at: string | null | undefined): string {
	if (!at) return '';
	const d = new Date(at.replace(' ', 'T'));
	if (Number.isNaN(d.getTime())) return at;
	return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

/** "2026-11-18" -> "Wed 18 Nov 2026" */
export function day(date: string): string {
	const d = new Date(`${date}T12:00:00`);
	if (Number.isNaN(d.getTime())) return date;
	return d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
}

export function seconds(s: number | null | undefined): string {
	if (s == null) return '';
	if (s < 90) return `${Math.round(s)}s`;
	return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

export function plural(n: number, word: string): string {
	return `${n} ${word}${n === 1 ? '' : word.endsWith('s') ? 'es' : 's'}`;
}
