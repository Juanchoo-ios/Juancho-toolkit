from __future__ import annotations

import asyncio
import threading

import toga
from toga.style import Pack
from toga.style.pack import COLUMN, ROW

from .catalog import SkinItem, load_catalogs
from .image_cache import ThumbnailCache
from .pipeline import filza_output_folder, process_skin


class BisayaToolkit(toga.App):
    def startup(self):
        self.items: list[SkinItem] = []
        self.cancelled = threading.Event()
        self.busy = False
        self.thumbnail_cache = ThumbnailCache()
        self.render_generation = 0

        self.search = toga.TextInput(placeholder="Search Heroes or Skins", on_change=self.render_items,
                                     style=Pack(flex=1, padding_right=8))
        self.refresh = toga.Button("↻", on_press=self.refresh_catalogs, style=Pack(width=48))
        top = toga.Box(children=[self.search, self.refresh], style=Pack(direction=ROW, padding=12))

        self.status = toga.Label("Loading downloadable packages…", style=Pack(padding=(4, 12)))
        self.progress = toga.ProgressBar(max=100, value=0, style=Pack(flex=1, padding=(4, 12, 12, 12)))
        self.cards = toga.Box(style=Pack(direction=COLUMN, padding=8))
        self.scroll = toga.ScrollContainer(content=self.cards, horizontal=False, style=Pack(flex=1))
        self.cancel_button = toga.Button("Cancel", on_press=self.cancel, enabled=False, style=Pack(padding=8))
        self.open_folder = toga.Button("Filza output: Documents/Bisaya Toolkit", on_press=self.show_output,
                                       style=Pack(padding=8))
        bottom = toga.Box(children=[self.cancel_button, self.open_folder], style=Pack(direction=ROW, padding=4))
        root = toga.Box(children=[top, self.status, self.scroll, self.progress, bottom],
                        style=Pack(direction=COLUMN, flex=1))

        self.main_window = toga.MainWindow(title="Bisaya Toolkit")
        self.main_window.content = root
        self.main_window.show()
        asyncio.create_task(self.refresh_catalogs(None))

    async def refresh_catalogs(self, _widget):
        if self.busy:
            return
        self.refresh.enabled = False
        self.status.text = "Loading catalogs…"
        try:
            self.items = await asyncio.to_thread(load_catalogs)
            self.status.text = f"Loaded {len(self.items)} downloadable packages."
            self.render_items(None)
        except Exception as exc:
            self.status.text = "Catalog loading failed."
            await self.main_window.error_dialog("Catalog error", str(exc))
        finally:
            self.refresh.enabled = True

    def render_items(self, _widget):
        self.render_generation += 1
        generation = self.render_generation
        query = self.search.value.strip().casefold()
        visible = [item for item in self.items if not query or query in f"{item.hero} {item.name}".casefold()][:90]
        self.cards.clear()
        for offset in range(0, len(visible), 3):
            row = toga.Box(style=Pack(direction=ROW, padding=2))
            for item in visible[offset:offset + 3]:
                image = toga.ImageView(None, style=Pack(width=108, height=108, padding=2))
                label = f"{item.name}\n{item.hero}"
                button = toga.Button(
                    label,
                    on_press=lambda widget, selected=item, **kwargs: asyncio.create_task(self.confirm_download(selected)),
                    style=Pack(height=58, padding=2),
                )
                card = toga.Box(children=[image, button], style=Pack(direction=COLUMN, flex=1, padding=3))
                row.add(card)
                if item.image_url:
                    asyncio.create_task(self.load_thumbnail(image, item.image_url, generation))
            self.cards.add(row)

    async def load_thumbnail(self, view, url: str, generation: int):
        try:
            path = await asyncio.to_thread(self.thumbnail_cache.get, url)
            if path and generation == self.render_generation:
                view.image = toga.Image(path)
        except Exception:
            # A missing preview never prevents downloading the actual package.
            pass

    async def confirm_download(self, item: SkinItem):
        if self.busy:
            return
        accepted = await self.main_window.question_dialog(
            "Download and convert",
            f"Convert {item.hero} - {item.name} to iOS?\n\nThe final ZIP will be saved in Documents/Bisaya Toolkit for Filza.",
        )
        if accepted:
            await self.run_pipeline(item)

    async def run_pipeline(self, item: SkinItem):
        self.busy = True
        self.cancelled.clear()
        self.cancel_button.enabled = True
        self.refresh.enabled = False
        loop = asyncio.get_running_loop()

        def update(value: float, message: str):
            loop.call_soon_threadsafe(self.update_progress, value, message)

        try:
            final_path, stats, extracted = await asyncio.to_thread(process_skin, item, self.cancelled, update)
            self.status.text = "Conversion complete. Temporary files deleted."
            await self.main_window.info_dialog(
                "Complete",
                f"Saved for Filza:\n{final_path}\n\nConverted bundles: {stats.bundles}\nExtracted files: {extracted}",
            )
        except InterruptedError:
            self.status.text = "Cancelled. Temporary files deleted."
        except Exception as exc:
            self.status.text = "Conversion failed. Temporary files deleted."
            await self.main_window.error_dialog("Conversion failed", str(exc))
        finally:
            self.busy = False
            self.cancel_button.enabled = False
            self.refresh.enabled = True

    def update_progress(self, value: float, message: str):
        self.progress.value = max(0, min(100, int(value * 100)))
        self.status.text = message

    def cancel(self, _widget):
        if self.busy:
            self.cancelled.set()
            self.status.text = "Cancelling safely…"

    async def show_output(self, _widget):
        await self.main_window.info_dialog("Filza output", str(filza_output_folder()))


def main():
    return BisayaToolkit("Bisaya Toolkit", "com.juancho.bisayatoolkit")
