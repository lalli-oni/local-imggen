import { api, type ProjectCard } from '$lib/api';
import type { PageLoad } from './$types';

export const load: PageLoad = async ({ fetch }) => api<{ projects: ProjectCard[] }>(fetch, 'projects');
