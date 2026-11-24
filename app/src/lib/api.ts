// Types and fetchers for `gen app serve`'s read-only JSON API (appserve.py).
import { error } from '@sveltejs/kit';

export type StageName = 'explore' | 'converge' | 'polish';
export type Rating = 'pick' | 'maybe' | 'reject' | null;

export interface Counts {
	pick: number;
	maybe: number;
	reject: number;
	unrated: number;
}

export interface RunImage {
	name: string;
	url: string | null;
	seed: number | null;
	blank?: boolean;
	scene?: string | null;
	prompt?: string;
	// run detail only
	full_prompt?: string | null;
	negative?: string | null;
	params?: Record<string, unknown> | null;
	created?: string | null;
	timing?: number | null;
}

export interface Daily {
	date: string | null;
	theme: string | null;
	from: string | null;
	props: string[] | null;
	cast: Record<string, number> | null;
	styles: string[] | null;
}

export interface Run {
	id: string;
	date: string;
	time: string;
	label: string;
	preset: string | null;
	styles: string[];
	prompt: string | null;
	count: number;
	images: RunImage[];
	transparent: boolean;
	sheet: string | null;
	timing: string | null;
	seconds: number | null;
	source: string | null;
	daily: Daily | null;
	wallpaper: boolean;
}

export interface Overview {
	projects: number;
	characters: number;
	runs: number;
	images: number;
	wallpaper: Run | null;
	recent: Run[];
}

export interface ProjectStage {
	character: string;
	stage: StageName;
}

export interface ProjectCard {
	slug: string;
	title: string;
	brief: string;
	version: number;
	cast: string[];
	stage: ProjectStage | null;
	passes: number;
	assets: number;
	stale: number;
	counts: Counts;
	cover: string | null;
	updated: string | null;
}

export interface Note {
	at: string;
	author: string;
	text: string;
}

export interface BoardImage {
	id: string;
	url: string | null;
	seed: number | null;
	rating: Rating;
	notes: Note[];
	prompt?: string;
}

export interface Pass {
	n: number;
	version: number;
	at: string;
	author: string;
	extra: string | null;
	ref: string | null;
	chars: string[] | null;
	stage: StageName | null;
	source: string | null;
	text: string | null;
	prompt: string | null;
	missing: boolean;
	sheet: string | null;
	images: BoardImage[];
}

export interface Project {
	slug: string;
	title: string;
	brief: string;
	version: number;
	generated: string;
	state: {
		prompt: string;
		negative: string | null;
		preset: string | null;
		params: Record<string, unknown>;
		palette: string[];
		cast: string[];
		refs: { name: string; role: string; url: string | null }[];
		docs: string[];
		snippets: Record<string, string>;
	};
	assets: { name: string; current: number; stale: boolean; reasons: string[]; url: string | null }[];
	stage: (ProjectStage & { stages?: Record<string, unknown>; error?: string }) | null;
	versions: {
		version: number;
		at: string;
		author: string;
		note: string;
		changed: string[];
		prompt: string;
		negative: string | null;
		preset: string | null;
	}[];
	passes: Pass[];
	others: { url: string | null; rating: Rating; notes: Note[] }[];
	notes: Note[];
	counts: Counts;
}

export interface CharacterCard {
	name: string;
	title: string;
	version: number;
	stage: StageName;
	description: string;
	palette: string[];
	ref: string | null;
	refs: number;
	projects: string[];
	updated: string | null;
}

export interface Ref {
	name: string;
	role: string;
	url: string | null;
}

export interface Character {
	name: string;
	title: string;
	version: number;
	stage: StageName;
	description: string;
	palette: string[];
	refs: Ref[];
	used_by: Record<string, { pin: number | null; assets: string[] }>;
	versions: {
		version: number;
		at: string;
		author: string;
		note: string;
		changed: string[];
		stage: StageName;
		description: string;
		refs: Ref[];
	}[];
}

export interface RunsPage {
	total: number;
	offset: number;
	limit: number;
	presets: string[];
	runs: Run[];
}

export interface LogLine {
	at: string | null;
	ok: boolean | null;
	text: string;
}

export interface Wallpapers {
	runs: Run[];
	log: LogLine[];
}

export interface ThemeRow {
	text: string;
	uses: number;
	last: string | null;
}

export interface Prompts {
	daily: {
		preset: string;
		style: string;
		cast: string[];
		no_repeat_days: number;
		props_per_day: number;
		count: number;
	};
	themes: {
		pool: ThemeRow[];
		dates: Record<string, ThemeRow[]>;
		months: Record<string, ThemeRow[]>;
		weekdays: Record<string, ThemeRow[]>;
	};
	props: Record<string, string[]>;
	styles: {
		name: string;
		description: string;
		suffix: string;
		negative_extra: string;
		post: string[];
		presets: string[];
		daily: boolean;
	}[];
	presets: {
		name: string;
		description: string;
		model: string | null;
		kind: 'image' | 'edit';
		styles: string[];
		suffix: string;
		negative_extra: string;
		post: string[];
		size: string | null;
		daily: boolean;
	}[];
	templates: {
		scene_prompt: { text: string; custom: boolean };
		cast_prompt: { text: string; custom: boolean };
		ref_prompt: { text: string; custom: boolean };
		stand_in: string;
		stand_ins: Record<string, string>;
	};
	stages: {
		stage: StageName;
		mode: string;
		preset: string;
		count: number;
		instruction: string;
		ideas: string[];
		needs_text: boolean;
	}[];
	characters: { name: string; title: string; stage: StageName; description: string; projects: string[] }[];
	projects: {
		slug: string;
		title: string;
		prompt: string;
		negative: string | null;
		preset: string | null;
		snippets: Record<string, string>;
		cast: string[];
		docs: string[];
	}[];
	errors: string[];
}

/** Runs per page on the runs list. */
export const RUNS_PAGE = 24;

type Fetch = typeof fetch;

/** GET an API path; a 404 or a dead server becomes SvelteKit's error page with the reason. */
export async function api<T>(fetch: Fetch, path: string): Promise<T> {
	let res: Response;
	try {
		res = await fetch(`/api/${path}`);
	} catch {
		error(503, 'Cannot reach gen app serve. Is it running on tulu?');
	}
	if (!res.ok) {
		let msg = res.statusText;
		try {
			msg = (await res.json()).error ?? msg;
		} catch {
			/* not JSON */
		}
		error(res.status, msg);
	}
	return res.json() as Promise<T>;
}

export function enc(part: string): string {
	return encodeURIComponent(part);
}
