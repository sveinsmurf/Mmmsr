# Multi Machine Minecraft Server Redundancy project

## Plans:

### Idea:

Experiment with redundancy for minecraft servers.
Where if one physical server fails the players connected won't notice anything.

The goal is to use COLO "**Co**arse-grained **lo**ck-stepping"

It is to run on a control pc, that then proxy's network requests to the worker pc's.

Those two worker pc's will have one primary and one seccondary pc.
They will both run the same minecraft server in parallel. 
When they both recive network signals they will both send back network singals tunneled through the main pc.
The main pc will then review the outgoing network signals and if they arent the same;
It will pause the main pc's minecraft server and send a direct copy of the process to the seccondary pc.
Hence why a determenistic or atleast a low-cpu cost minecraft server would be ideal.

The primary pc will proably also need a backup as that would be a single point of failure.

### Server setup:

My setup includes Mini-PC's with these specs:

- CPU: Intel Core i5-3470T 2c/4t
- HDD: 300GB WD3200BEKT SATA
- RAM: 7.6GiB

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

### Way out plan:

Make the i5-9500 system host a PXE/iPXE server to setup the mini pc's faster and more centralised.
