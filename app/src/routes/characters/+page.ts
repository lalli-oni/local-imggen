import { api, type CharacterCard } from '$lib/api';
import type { PageLoad } from './$types';

export const load: PageLoad = async ({ fetch }) => api<{ characters: CharacterCard[] }>(fetch, 'characters');
