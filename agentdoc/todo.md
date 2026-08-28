# Migrate to PostgreSQL and lightweight search
ArangoDB seemed awesome at the time when I was quite excited about graph databases - but it is a heavyweight and PostgreSQL is capable of essentially everything we are asking ArangoDB to do. We have spent significant effort optimizing queries, without a lot of understanding the optimizations effectively, to work around its less fundamental designs. ElasticSearch is similarly nice, but also very heavy for essentially a static search store. We can operate a variety of other lighter softwares. PostgreSQL might even have enough of a fulltext capability to handle this as-is and further simplify our operations, or tantivy, etc.

# Data Paths
Data paths are currently insufficient for tracking the potential drift between implementations. For instance if NX-OS and IOS XR somehow include the same YANG xpath with different descriptions or type then we would fail to catch this condition effectively. We can make data paths unique across machine_id, human_id, and data_type - and keep data_path_source descriptions etc. We will maintain integer keys and futz with larger natural key relationships like this outside of it.

# Correctness
There are plenty of duplicate OIDs cited in various MIBs - are we dropping these? Undetermined. Need to figure out the current and correct behavior. What other issues of correctness do we have?

# Pre-flatten
TDM resulted in the pyang flatten plugin which we should likely prefer instead of the current parsing logic. The best outcome is our own fork of the yang models repo which contains patches for incomplete sets of the yang models, and then a CI-like process kicks off which flattens the yang sets in to the flat files we want to reference, and then etl just pulls down the flattened files for parsing rather than expending all this compute re-processing each time. iirc flatten produces the same desired information - and even better it can use the netconf hello!

# ETL Optimization
ETL is currently very slow and compute intensive. Memory intensiveness has been reduced via not keeping a massive cache nor every version in memory. Per OS version we need roughly 5 GiB of RAM for the parsing. This is paralellizable, and we can likely increase efficiency by batching the inserts which I believe is likely not happening today.

# Versioning
Dependencies are poorly versioned today. Yikes! Lock it down.

# Rootless Porting
Don't default to low numbered ports. Start ports in 35k+.

# CI
Current ETL is dumb. It was cute when I was just PoC'ing. We need better checkpointing mechanisms to determine what makes sense to run and able to delta in new additions.

# Integrate with TimescaleDB
If we want to get really weird let's take measurements via timescaledb?

