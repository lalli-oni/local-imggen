import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig, loadEnv } from 'vite';

// `npm run dev` on :5173 talks to `gen app serve` (default :8191) for data and images.
// IMAGEGEN_APP_API=http://127.0.0.1:PORT points it elsewhere.
export default defineConfig(({ mode }) => {
	const api = loadEnv(mode, '.', 'IMAGEGEN_').IMAGEGEN_APP_API || 'http://127.0.0.1:8191';
	const target = { target: api, headers: { host: '127.0.0.1' } };
	return {
		plugins: [sveltekit()],
		server: { proxy: { '/api': target, '/files': target } }
	};
});
