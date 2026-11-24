import { api, type Prompts } from '$lib/api';
import type { PageLoad } from './$types';

export const load: PageLoad = async ({ fetch }) => api<Prompts>(fetch, 'prompts');
