import os
import unittest

from sts.ingest import load_book, UnsupportedFormat
from sts.ingest.txt import split_chapters, strip_gutenberg
from sts.segment import segment_book

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


class IngestTests(unittest.TestCase):
    def test_txt_gutenberg(self):
        b = load_book(os.path.join(FIX, "mini.txt"))
        self.assertEqual(b.title, "Mini Alice")
        self.assertEqual(b.author, "Test Author")
        self.assertEqual(len(b.chapters), 4)
        self.assertTrue(b.chapters[0].title.startswith("CHAPTER I."))
        self.assertNotIn("PROJECT GUTENBERG", b.chapters[-1].text)
        self.assertNotIn("Contents", b.chapters[0].text)
        self.assertEqual(len(b.source_sha256), 64)

    def test_epub(self):
        b = load_book(os.path.join(FIX, "mini.epub"))
        self.assertEqual(b.title, "Mini Alice EPUB")
        self.assertEqual(len(b.chapters), 4)
        self.assertGreater(b.words, 4000)
        self.assertNotIn("<p>", b.chapters[0].text)

    def test_slice(self):
        b = load_book(os.path.join(FIX, "mini.txt"))
        self.assertEqual(len(b.slice_chapters("2-3").chapters), 2)
        self.assertEqual(len(b.slice_chapters("3-").chapters), 2)
        self.assertEqual(len(b.slice_chapters("2").chapters), 1)
        with self.assertRaises(ValueError):
            b.slice_chapters("9-12")

    def test_unsupported(self):
        p = os.path.join(FIX, "mini.txt")
        with self.assertRaises(UnsupportedFormat):
            load_book(p, filename="book.pdf")

    def test_toc_duplicates_dropped(self):
        text = "Contents\n\nCHAPTER I.\n\nCHAPTER II.\n\n" + "CHAPTER I.\n\n" + ("word " * 100) + "\n\nCHAPTER II.\n\n" + ("word " * 100)
        chs = split_chapters(text)
        self.assertEqual([c.title for c in chs], ["CHAPTER I.", "CHAPTER II."])

    def test_strip_gutenberg_meta(self):
        t, meta = strip_gutenberg("Title: X\nAuthor: Y\n*** START OF THE PROJECT GUTENBERG EBOOK X ***\nbody\n*** END OF THE PROJECT GUTENBERG EBOOK X ***\nlicense")
        self.assertEqual(meta["title"], "X")
        self.assertEqual(t.strip(), "body")


class SegmentTests(unittest.TestCase):
    def test_segments_respect_size_and_chapters(self):
        b = load_book(os.path.join(FIX, "mini.txt"))
        scenes = segment_book(b, scene_tokens=600)
        self.assertGreater(len(scenes), 4)
        for s in scenes:
            self.assertLessEqual(s.tokens, int(600 * 1.35) + 300)  # a paragraph may overshoot slightly
        # never split mid paragraph: every scene ends where a paragraph ends
        for s in scenes:
            self.assertTrue(s.text.strip().endswith(")"))
        self.assertEqual(sorted({s.chapter for s in scenes}), [1, 2, 3, 4])
        joined = " ".join(s.text for s in scenes).split()
        self.assertEqual(len(joined), b.words)
        self.assertEqual(scenes[0].id, "c001")

    def test_max_scenes(self):
        b = load_book(os.path.join(FIX, "mini.txt"))
        self.assertEqual(len(segment_book(b, scene_tokens=400, max_scenes=3)), 3)


if __name__ == "__main__":
    unittest.main()
