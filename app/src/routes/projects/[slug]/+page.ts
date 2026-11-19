import { api, enc, type Project } from '$lib/api';
import type { PageLoad } from './$types';

export const load: PageLoad = async ({ fetch, params }) => ({
	project: await api<Project>(fetch, `projects/${enc(params.slug)}`)
});
