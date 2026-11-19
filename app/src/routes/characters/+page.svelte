<script lang="ts">
	import CharacterTile from '$lib/CharacterTile.svelte';

	let { data } = $props();
</script>

<svelte:head><title>Characters · imagegen</title></svelte:head>

<div class="page">
	<h1>Characters</h1>
	{#if data.characters.length}
		<div class="cards">
			{#each data.characters as c (c.name)}<CharacterTile {c} />{/each}
		</div>
	{:else}
		<p class="empty">No characters yet. <code>gen char new NAME --description "..."</code></p>
	{/if}
</div>
