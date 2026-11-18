import { api, RUNS_PAGE, type RunsPage } from '$lib/api';
import type { PageLoad } from './$types';

export const load: PageLoad = async ({ fetch, url }) => {
	const q = new URLSearchParams();
	for (const k of ['preset', 'q', 'wallpaper']) {
		const v = url.searchParams.get(k);
		if (v) q.set(k, v);
	}
	q.set('limit', String(RUNS_PAGE));
	return { page: await api<RunsPage>(fetch, `runs?${q}`), query: q.toString() };
};
