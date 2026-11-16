#!/usr/bin/env python3
"""Measure soft-dirty page rate of a process over an interval."""

import ctypes
import os
import struct
import sys
import time

PAGE_SIZE = 4096
SOFT_DIRTY_BIT = 1 << 55
PAGEMAP_ENTRY = 8

def clear_soft_dirty(pid):
    fd = os.open(f"/proc/{pid}/clear_refs", os.O_WRONLY)
    try:
        os.write(fd, b"4")
    finally:
        os.close(fd)

def count_dirty_pages(pid, pagemap=None):
    path = f"/proc/{pid}/pagemap"
    if pagemap is None:
        pagemap = open(path, "rb", buffering=0)
    pagemap.seek(0)
    count = 0
    total = 0
    chunk = 1 << 20
    while True:
        data = pagemap.read(chunk * PAGEMAP_ENTRY)
        if not data:
            break
        for i in range(0, len(data), PAGEMAP_ENTRY):
            entry = struct.unpack_from("<Q", data, i)[0]
            if entry & SOFT_DIRTY_BIT:
                count += 1
            total += 1
    return count, total

def main():
    if len(sys.argv) < 3:
        print("usage: dirtyrate.py <pid> <seconds> [interval]")
        sys.exit(1)
    pid = int(sys.argv[1])
    seconds = float(sys.argv[2])
    interval = float(sys.argv[3]) if len(sys.argv) > 3 else seconds
    clear_soft_dirty(pid)
    pagemap = open(f"/proc/{pid}/pagemap", "rb", buffering=0)
    start = time.time()
    measurements = []
    while time.time() - start < seconds:
        time.sleep(interval)
        count, total = count_dirty_pages(pid, pagemap)
        elapsed = interval
        measurements.append((time.time(), count, total))
        clear_soft_dirty(pid)
    pagemap.close()
    print(f"{'time':>12} {'dirty_pages':>12} {'dirty_mb':>10} {'mb/s':>10} {'total_pages':>12}")
    for t, count, total in measurements:
        mb = count * PAGE_SIZE / 1024 / 1024
        mbps = mb / interval
        print(f"{t - start:12.1f} {count:12d} {mb:10.1f} {mbps:10.1f} {total:12d}")

if __name__ == "__main__":
    main()
