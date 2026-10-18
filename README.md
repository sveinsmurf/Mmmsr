# Multi Machine Minecraft Server Redundancy project

## Plans:

### Idea:

Experiment with redundancy for minecraft servers.
Where if one physical server fails the players connected won't notice anything.

The goal is to use COLO "**Co**arse-grained **lo**ck-stepping"

### Server setup:



### For server:

Test these server options:

1. https://github.com/ferrumc-rs/ferrumc/
    1. Is a rust-based low resource usage limited minecraft server
    2. Might work as low cpu usage means less data to send over
    3. Not meant for "normal" survival servers but for 
2. https://github.com/vertoforce/detmc.git
    1. Is a determenistic minecraft server
    2. COLO works best with determenistic tasks
    3. Seems to be entirely unmaintained and vibe coded
    4. Is a research tool not meant for players
3. https://github.com/Pumpkin-MC/Pumpkin.git
    1. Another rust-based low resource usage minecraft server
    2. Great for the same reasons as FerrumC but more complete as a server
    3. Might have too many features compared to FerrumC
4. https://github.com/PaperMC/Paper.git
    1. If all-else fails, theres always paper
