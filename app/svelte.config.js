import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
export default {
	preprocess: vitePreprocess(),
	kit: {
		// a single-page app: gen app serve answers every client-side route with index.html
		adapter: adapter({ fallback: 'index.html' })
	}
};
