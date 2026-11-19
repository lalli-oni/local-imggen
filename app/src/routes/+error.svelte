<script lang="ts">
	import { page } from '$app/state';
</script>

<div class="empty">
	<h1>{page.status}</h1>
	<p>{page.error?.message}</p>
	{#if page.status === 503}
		<p class="small">On tulu: <code>gen app serve</code></p>
	{/if}
	<p><a href="/">Back home</a></p>
</div>
