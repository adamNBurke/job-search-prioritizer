"""Parsed, never executed. Lines ending in BAD must be flagged; lines ending in OK must not."""

import codecs
import gzip
import io
import subprocess
import tempfile
from pathlib import Path

path = Path("x")

open("a.txt")  # BAD no encoding
open("a.txt", "w", encoding="utf8")  # BAD wrong literal
open("a.txt", "r", encoding="cp1252")  # BAD wrong literal
io.open("a.txt")  # BAD io.open without encoding
codecs.open("a.txt", "r")  # BAD codecs.open without encoding
path.read_text()  # BAD no encoding
path.write_text("x")  # BAD no encoding
path.open("w")  # BAD Path.open text mode without encoding
tempfile.NamedTemporaryFile("w")  # BAD text temp file without encoding
subprocess.run(["x"], text=True)  # BAD text mode without encoding
subprocess.check_output(["x"], universal_newlines=True)  # BAD text mode without encoding

open("a.bin", "rb")  # OK binary mode
gzip.open("a.gz", "rb")  # OK binary mode
tempfile.NamedTemporaryFile()  # OK default mode is binary
open("a.txt", encoding="utf-8")  # OK
open("a.txt", "w", encoding="utf-8-sig")  # OK
path.read_text(encoding="utf-8")  # OK
subprocess.run(["x"], text=True, encoding="utf-8")  # OK
subprocess.run(["x"], capture_output=True)  # OK bytes
