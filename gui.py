"""
Basic GUI for the Ugaoo scraper + uploader.

    python gui.py

Four steps, in order:
  1. Scrape Ugaoo Plants (8 pages)  -> writes CSV (incl. slug + image url)
  2. Upload Products to Database    -> POSTs each row to your API
  3. Download Images                -> product_images/<slug>.<ext>
  4. Upload Images to Database      -> POST /api/products/{id}/images
"""
import logging
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import config
import image_downloader
import image_uploader
import scraper
import uploader
import variant_image_uploader
import variant_images


class QueueHandler(logging.Handler):
    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        self.log_queue.put(self.format(record))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Ugaoo Plants — Scraper & Uploader")
        self.geometry("1040x660")
        self.minsize(700, 480)

        self.log_queue: queue.Queue = queue.Queue()
        self.csv_path = tk.StringVar(value=config.CSV_PATH)
        self.api_url = tk.StringVar(value=config.CREATE_PRODUCT_ENDPOINT)
        self.category_id = tk.StringVar(value=str(config.CATEGORY_ID))
        self.variants_csv = tk.StringVar(value=config.VARIANTS_CSV)
        self.provider = tk.StringVar(value=config.IMAGE_PROVIDER)
        self.gen_limit = tk.StringVar(value=str(config.MAX_IMAGES_PER_RUN))

        self._build_ui()
        self._setup_logging()
        self.after(200, self._poll_log_queue)

    # ------------------------------------------------------------------
    def _build_ui(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="CSV file:").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.csv_path).grid(row=0, column=1, sticky="we", padx=5)
        ttk.Button(top, text="Browse", command=self._browse_csv).grid(row=0, column=2)

        ttk.Label(top, text="API endpoint:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(top, textvariable=self.api_url).grid(row=1, column=1, sticky="we", padx=5, pady=(6, 0))

        ttk.Label(top, text="Category ID:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(top, textvariable=self.category_id, width=10).grid(row=2, column=1, sticky="w", padx=5, pady=(6, 0))

        btns = ttk.Frame(self, padding=10)
        btns.pack(fill="x")
        self.scrape_btn = ttk.Button(btns, text="1. Scrape Ugaoo Plants (8 pages)", command=self._start_scrape)
        self.scrape_btn.pack(side="left", padx=5)
        self.upload_btn = ttk.Button(btns, text="2. Upload Products to Database", command=self._start_upload)
        self.upload_btn.pack(side="left", padx=5)
        self.dl_img_btn = ttk.Button(btns, text="3. Download Images", command=self._start_download_images)
        self.dl_img_btn.pack(side="left", padx=5)
        self.up_img_btn = ttk.Button(btns, text="4. Upload Images to Database", command=self._start_upload_images)
        self.up_img_btn.pack(side="left", padx=5)

        row2 = ttk.Frame(self, padding=(10, 0, 10, 10))
        row2.pack(fill="x")
        ttk.Label(row2, text="Variants CSV:").pack(side="left")
        ttk.Entry(row2, textvariable=self.variants_csv, width=22).pack(side="left", padx=4)
        ttk.Label(row2, text="Provider:").pack(side="left", padx=(8, 0))
        ttk.Combobox(row2, textvariable=self.provider, values=["cloudflare", "openai"], width=10,
                     state="readonly").pack(side="left", padx=4)
        ttk.Label(row2, text="Max/run (0=all):").pack(side="left", padx=(8, 0))
        ttk.Entry(row2, textvariable=self.gen_limit, width=5).pack(side="left", padx=4)
        self.gen_var_btn = ttk.Button(row2, text="5. Generate Variant Images", command=self._start_generate_variants)
        self.gen_var_btn.pack(side="left", padx=(12, 4))
        self.up_var_btn = ttk.Button(row2, text="6. Upload Variant Images", command=self._start_upload_variants)
        self.up_var_btn.pack(side="left", padx=4)

        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=10, pady=(0, 6))

        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(self, textvariable=self.status_var).pack(anchor="w", padx=10)

        self.log_text = tk.Text(self, height=25, state="disabled", bg="#111318", fg="#7CFC7C")
        self.log_text.pack(fill="both", expand=True, padx=10, pady=10)

    def _browse_csv(self):
        path = filedialog.asksaveasfilename(
            filetypes=[("CSV files", "*.csv")], defaultextension=".csv",
            initialfile=self.csv_path.get(),
        )
        if path:
            self.csv_path.set(path)

    # ------------------------------------------------------------------
    def _setup_logging(self):
        handler = QueueHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.INFO)

    def _poll_log_queue(self):
        while not self.log_queue.empty():
            msg = self.log_queue.get_nowait()
            self.log_text.configure(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.after(200, self._poll_log_queue)

    # ------------------------------------------------------------------
    def _start_scrape(self):
        self.scrape_btn.configure(state="disabled")
        self.status_var.set("Scraping...")
        self.progress.configure(mode="indeterminate")
        self.progress.start(50)
        threading.Thread(target=self._run_scrape, daemon=True).start()

    def _run_scrape(self):
        try:
            path = self.csv_path.get()
            plants = scraper.scrape_all()
            scraper.save_csv(plants, path)
            self.status_var.set(f"Done: {len(plants)} plants saved to {path}")
        except Exception as exc:  # noqa: BLE001
            logging.exception("Scrape failed")
            self.status_var.set("Scrape failed - see log")
            messagebox.showerror("Scrape failed", str(exc))
        finally:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.scrape_btn.configure(state="normal")

    def _start_upload(self):
        self.upload_btn.configure(state="disabled")
        self.status_var.set("Uploading...")
        threading.Thread(target=self._run_upload, daemon=True).start()

    def _run_upload(self):
        try:
            config.CREATE_PRODUCT_ENDPOINT = self.api_url.get()
            try:
                config.CATEGORY_ID = int(self.category_id.get())
            except ValueError:
                config.CATEGORY_ID = None

            path = self.csv_path.get()
            rows = uploader.load_rows(path)
            self.progress.configure(maximum=len(rows), value=0)

            def cb(i, total, outcome):
                self.progress.configure(value=i)
                self.status_var.set(f"Uploading {i}/{total}")

            results = uploader.bulk_upload(path, progress_cb=cb)
            self.status_var.set(
                f"Upload done: {results['success']} created, {results['skipped']} already existed, {results['failed']} failed"
            )
            if results["failed"]:
                messagebox.showwarning(
                    "Some uploads failed",
                    f"{results['failed']} row(s) failed. See {config.FAILED_UPLOADS_CSV}",
                )
            else:
                messagebox.showinfo(
                    "Upload complete",
                    f"{results['success']} created, {results['skipped']} already existed.",
                )
        except Exception as exc:  # noqa: BLE001
            logging.exception("Upload failed")
            self.status_var.set("Upload failed - see log")
            messagebox.showerror("Upload failed", str(exc))
        finally:
            self.upload_btn.configure(state="normal")

    # ------------------------------------------------------------------
    def _apply_api_settings(self):
        config.CREATE_PRODUCT_ENDPOINT = self.api_url.get()
        try:
            config.CATEGORY_ID = int(self.category_id.get())
        except ValueError:
            config.CATEGORY_ID = None

    def _run_in_thread(self, button, working_text, fn):
        button.configure(state="disabled")
        self.status_var.set(working_text)

        def runner():
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                logging.exception("%s failed", working_text)
                self.status_var.set(f"{working_text} failed - see log")
                messagebox.showerror("Error", str(exc))
            finally:
                button.configure(state="normal")

        threading.Thread(target=runner, daemon=True).start()

    def _progress(self, label):
        def cb(i, total, outcome):
            self.progress.configure(maximum=max(total, 1), value=i)
            self.status_var.set(f"{label} {i}/{total}")
        return cb

    def _start_download_images(self):
        def work():
            res = image_downloader.download_all(self.csv_path.get(), progress_cb=self._progress("Downloading images"))
            self.status_var.set(
                f"Images: {res['downloaded']} downloaded, {res['skipped']} already on disk, {res['failed']} failed"
            )
        self._run_in_thread(self.dl_img_btn, "Downloading images...", work)

    def _start_upload_images(self):
        def work():
            self._apply_api_settings()
            res = image_uploader.upload_all_images(self.csv_path.get(), progress_cb=self._progress("Uploading images"))
            self.status_var.set(
                f"Images: {res['uploaded']} uploaded, {res['skipped']} skipped, {res['failed']} failed"
            )
            if res["failed"] or res["no_product"] or res["no_image"]:
                messagebox.showwarning(
                    "Image upload finished with issues",
                    f"{res['failed']} failed, {res['no_product']} slug(s) not found in API, "
                    f"{res['no_image']} without a downloaded image. See the log"
                    + (f" and {config.FAILED_IMAGE_UPLOADS_CSV}." if res["failed"] else "."),
                )
            else:
                messagebox.showinfo("Image upload complete", f"{res['uploaded']} images uploaded.")
        self._run_in_thread(self.up_img_btn, "Uploading images...", work)


    def _start_generate_variants(self):
        def work():
            try:
                limit = int(self.gen_limit.get())
            except ValueError:
                limit = None
            res = variant_images.generate_all(
                self.variants_csv.get(), self.provider.get(), limit,
                progress_cb=self._progress("Generating images"),
            )
            note = " (quota reached - run again later)" if res["quota_hit"] else ""
            no_ref = f", {res['no_reference']} skipped (no product photo)" if res.get("no_reference") else ""
            self.status_var.set(f"Generated {res['generated']}, failed {res['failed']}{no_ref}{note}")
        self._run_in_thread(self.gen_var_btn, "Generating variant images...", work)

    def _start_upload_variants(self):
        def work():
            self._apply_api_settings()
            res = variant_image_uploader.upload_all_variant_images(
                self.variants_csv.get(), progress_cb=self._progress("Uploading variants"))
            self.status_var.set(f"Variant upload: {res['variants_ok']} ok ({res['uploaded_images']} images), "
                                f"{res['variants_failed']} failed")
            if res["variants_failed"]:
                messagebox.showwarning("Some variants failed",
                                       f"{res['variants_failed']} variant(s) failed. See {config.FAILED_VARIANT_UPLOADS_CSV}")
        self._run_in_thread(self.up_var_btn, "Uploading variant images...", work)


if __name__ == "__main__":
    App().mainloop()
