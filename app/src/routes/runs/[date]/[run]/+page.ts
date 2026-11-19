import { api, enc, type Run } from '$lib/api';
import type { PageLoad } from './$types';

export const load: PageLoad = async ({ fetch, params }) => ({
	run: await api<Run>(fetch, `runs/${enc(params.date)}/${enc(params.run)}`)
});
