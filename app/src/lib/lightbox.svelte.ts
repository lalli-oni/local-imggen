// One full-screen viewer for every page: open(items, i) from any thumbnail.
export interface LightboxItem {
	url: string;
	title: string;
	caption?: string;
	href?: string; // "more about this image" (a run page)
}

class LightboxState {
	items = $state<LightboxItem[]>([]);
	index = $state(-1);

	get current(): LightboxItem | null {
		return this.items[this.index] ?? null;
	}

	open(items: LightboxItem[], index = 0) {
		this.items = items;
		this.index = index;
	}

	close() {
		this.index = -1;
	}

	step(by: number) {
		if (!this.items.length) return;
		this.index = (this.index + by + this.items.length) % this.items.length;
	}
}

export const lightbox = new LightboxState();
