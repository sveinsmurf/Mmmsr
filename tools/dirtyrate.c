#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#define SOFT_DIRTY (1UL << 55)
#define PAGE_ENTRIES (256 * 1024)

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: %s <pid> <seconds> [interval]\n", argv[0]);
        return 1;
    }
    pid_t pid = atoi(argv[1]);
    double duration = atof(argv[2]);
    double interval = argc > 3 ? atof(argv[3]) : duration;

    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/pagemap", pid);
    int pagemap = open(path, O_RDONLY);
    if (pagemap < 0) { perror("open pagemap"); return 1; }

    snprintf(path, sizeof(path), "/proc/%d/clear_refs", pid);
    int clear = open(path, O_WRONLY);
    if (clear < 0) { perror("open clear_refs"); return 1; }

    uint64_t *buf = mmap(NULL, PAGE_ENTRIES * sizeof(uint64_t),
                         PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (buf == MAP_FAILED) { perror("mmap"); return 1; }

    double t0 = now_s();
    printf("%8s %12s %10s %10s %12s\n", "time", "dirty_pages", "dirty_mb", "mb/s", "total_pages");
    while (now_s() - t0 < duration) {
        usleep((useconds_t)(interval * 1e6));
        if (write(clear, "4", 1) != 1) { perror("clear_refs"); break; }

        off_t pos = 0;
        ssize_t n;
        uint64_t dirty = 0, total = 0;
        while ((n = pread(pagemap, buf, PAGE_ENTRIES * sizeof(uint64_t), pos)) > 0) {
            size_t entries = n / sizeof(uint64_t);
            for (size_t i = 0; i < entries; i++) {
                total++;
                if (buf[i] & SOFT_DIRTY) dirty++;
            }
            pos += n;
        }
        double mb = dirty * 4096.0 / (1024.0 * 1024.0);
        printf("%8.1f %12lu %10.1f %10.1f %12lu\n",
               now_s() - t0, dirty, mb, mb / interval, total);
        fflush(stdout);
    }
    munmap(buf, PAGE_ENTRIES * sizeof(uint64_t));
    close(pagemap);
    close(clear);
    return 0;
}
