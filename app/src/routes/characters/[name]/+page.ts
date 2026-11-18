import { api, enc, type Character } from '$lib/api';
import type { PageLoad } from './$types';

export const load: PageLoad = async ({ fetch, params }) => ({
	character: await api<Character>(fetch, `characters/${enc(params.name)}`)
});
