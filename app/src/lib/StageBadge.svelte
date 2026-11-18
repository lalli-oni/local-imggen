<script lang="ts">
	import type { StageName } from './api';

	let { stage, character }: { stage: StageName; character?: string } = $props();
	const hint: Record<StageName, string> = {
		explore: 'Explore: trying directions, off-model is fine',
		converge: 'Converge: closing in on one look',
		polish: 'Polish: fixing details on the chosen look'
	};
</script>

<span class="stage {stage}" title={hint[stage]}>
	{#if character}<span class="who">{character}</span>{/if}{stage}
</span>

<style>
	.stage {
		display: inline-flex;
		gap: 6px;
		align-items: center;
		padding: 2px 9px;
		border-radius: 999px;
		font-size: 12px;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		border: 1px solid currentColor;
		white-space: nowrap;
	}
	.who {
		text-transform: none;
		font-weight: 500;
		letter-spacing: 0;
		opacity: 0.8;
	}
	.explore {
		color: var(--cyan);
	}
	.converge {
		color: var(--violet);
	}
	.polish {
		color: var(--gold);
	}
</style>
