import { api, type CharacterCard, type Overview, type ProjectCard } from '$lib/api';
import type { PageLoad } from './$types';

export const load: PageLoad = async ({ fetch }) => {
	const [overview, projects, characters] = await Promise.all([
		api<Overview>(fetch, 'overview'),
		api<{ projects: ProjectCard[] }>(fetch, 'projects'),
		api<{ characters: CharacterCard[] }>(fetch, 'characters')
	]);
	return { overview, projects: projects.projects, characters: characters.characters };
};
