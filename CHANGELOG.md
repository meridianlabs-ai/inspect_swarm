# Changelog

## [0.2.1](https://github.com/meridianlabs-ai/inspect_swarm/compare/0.2.0...0.2.1) (2026-10-28)


### Features

* raise shared_memory's default size limits ([#110](https://github.com/meridianlabs-ai/inspect_swarm/issues/110)) ([114cf7a](https://github.com/meridianlabs-ai/inspect_swarm/commit/114cf7a4c3574546da7d1bbbffd28bb029cadce5))
* the changes command names every changed file ([#109](https://github.com/meridianlabs-ai/inspect_swarm/issues/109)) ([f84ee01](https://github.com/meridianlabs-ai/inspect_swarm/commit/f84ee01bc37ce52ee73a65f1903a851f06989e0d))
* the shared memory preamble puts tool names, paths and ids in markdown backticks ([#99](https://github.com/meridianlabs-ai/inspect_swarm/issues/99)) ([ab8bd2c](https://github.com/meridianlabs-ai/inspect_swarm/commit/ab8bd2c008231a72351e60fd74f5530f32cab2d0))

## [0.2.0](https://github.com/meridianlabs-ai/inspect_swarm/compare/0.1.0...0.2.0) (2026-10-27)


### ⚠ BREAKING CHANGES

* remove swarm notices; members learn of shared memory changes from tools ([#106](https://github.com/meridianlabs-ai/inspect_swarm/issues/106))

### Features

* new `changes` command in `shared_memory` reports changed files without listing the board ([#96](https://github.com/meridianlabs-ai/inspect_swarm/issues/96)) ([f56b83e](https://github.com/meridianlabs-ai/inspect_swarm/commit/f56b83e10a7349ab923af3635ccc4d916949dc29))
* remove swarm notices; members learn of shared memory changes from tools ([#106](https://github.com/meridianlabs-ai/inspect_swarm/issues/106)) ([aa9ec88](https://github.com/meridianlabs-ai/inspect_swarm/commit/aa9ec882cbbb3eda8ef8c7923214bbde4eecac6c))
* shared memory accepts text that looks like a fence, and the docs say peer text has no protection ([#105](https://github.com/meridianlabs-ai/inspect_swarm/issues/105)) ([b5ae470](https://github.com/meridianlabs-ai/inspect_swarm/commit/b5ae470502e2356a5392e50deb5c052f97a56c14))
* shared memory drops the repeated not-instructions headers ([#83](https://github.com/meridianlabs-ai/inspect_swarm/issues/83)) ([ce481c6](https://github.com/meridianlabs-ai/inspect_swarm/commit/ce481c6a781e7db4cf003eb4fbe76394f443fdbd))
* shared memory fences drop the record and line-range attributes ([#86](https://github.com/meridianlabs-ai/inspect_swarm/issues/86)) ([2d341af](https://github.com/meridianlabs-ai/inspect_swarm/commit/2d341afb55c6c5326301f305fc00c2a66250e190))
* shared memory fences name the last writer as last_writer ([#89](https://github.com/meridianlabs-ai/inspect_swarm/issues/89)) ([2b8c740](https://github.com/meridianlabs-ai/inspect_swarm/commit/2b8c7404a531f21eab2d040bacc37beeb81e4b42))
* shared memory has no upper bounds, and Inspect truncates its tools' results ([#93](https://github.com/meridianlabs-ai/inspect_swarm/issues/93)) ([4f0657c](https://github.com/meridianlabs-ai/inspect_swarm/commit/4f0657c8cdab41e3d4744728c79a043770a05a58))
* shared memory views and waits take a file id as file_id ([#90](https://github.com/meridianlabs-ai/inspect_swarm/issues/90)) ([d9092e5](https://github.com/meridianlabs-ai/inspect_swarm/commit/d9092e50a67a4810875d25a7ceb6afe2a38eacba))
* swarm notices put tool calls in markdown backticks ([#94](https://github.com/meridianlabs-ai/inspect_swarm/issues/94)) ([0701835](https://github.com/meridianlabs-ai/inspect_swarm/commit/070183577877dae13b254f7e280b2f46b92a210f))
* swarm notices start with [Automatic swarm notice] ([#98](https://github.com/meridianlabs-ai/inspect_swarm/issues/98)) ([cbe8b26](https://github.com/meridianlabs-ai/inspect_swarm/commit/cbe8b264b5486952cfdbc3a297fa03ee6b811c65))


### Bug Fixes

* shared memory listings line up their columns when file ids, versions or writers differ in length ([#97](https://github.com/meridianlabs-ai/inspect_swarm/issues/97)) ([6345c82](https://github.com/meridianlabs-ai/inspect_swarm/commit/6345c82ea8ca1cca956ebc4612a610fddb59e491))
* validate member limit dicts ([#88](https://github.com/meridianlabs-ai/inspect_swarm/issues/88)) ([fff3c7e](https://github.com/meridianlabs-ai/inspect_swarm/commit/fff3c7ee88be70a05b5d72531db072c7ac8b452b))

## 0.1.0 (2026-10-23)


### Features

* an agent-raised custom limit ends only that member ([#44](https://github.com/meridianlabs-ai/inspect_swarm/issues/44)) ([760deee](https://github.com/meridianlabs-ai/inspect_swarm/commit/760deee5c4d423ef504679eba9761a5a5af18915))
* an unsubmitted status for members that stop on a content filter or length limit ([#39](https://github.com/meridianlabs-ai/inspect_swarm/issues/39)) ([a4748b4](https://github.com/meridianlabs-ai/inspect_swarm/commit/a4748b4804e88c5687662fbe6015189c4d205d9c))
* background deepagent members ([#50](https://github.com/meridianlabs-ai/inspect_swarm/issues/50)) ([c707e4a](https://github.com/meridianlabs-ai/inspect_swarm/commit/c707e4acbb18062deef9932ea49245485367ea1b))
* bind swarm tools to a member's loop, with channel reach for its subagents ([#42](https://github.com/meridianlabs-ai/inspect_swarm/issues/42)) ([3d6614b](https://github.com/meridianlabs-ai/inspect_swarm/commit/3d6614bde493ae12fe2036a30811db62fd4a59ac))
* budget splits across swarm members ([#30](https://github.com/meridianlabs-ai/inspect_swarm/issues/30)) ([dd7307b](https://github.com/meridianlabs-ai/inspect_swarm/commit/dd7307bed580c523b879d6f8e6f2657111c673f2))
* choose the swarm's answer separately from when it stops: first, last or the controller's choice ([#71](https://github.com/meridianlabs-ai/inspect_swarm/issues/71)) ([80a18d6](https://github.com/meridianlabs-ai/inspect_swarm/commit/80a18d688f0480689a6941f4bc27c746bcf51001))
* export the swarm ledger's models from inspect_swarm.analysis ([#55](https://github.com/meridianlabs-ai/inspect_swarm/issues/55)) ([9da50f6](https://github.com/meridianlabs-ai/inspect_swarm/commit/9da50f6e7110d66eeb02ad7c4855749524db187a))
* filesystem(shared_volume=True) for per-member sandboxes ([#54](https://github.com/meridianlabs-ai/inspect_swarm/issues/54)) ([5cfbbb5](https://github.com/meridianlabs-ai/inspect_swarm/commit/5cfbbb5762cbb7eed3b4dc7cd24128ca4d70f44b))
* finalise under the final meter and record what ended the swarm ([#38](https://github.com/meridianlabs-ai/inspect_swarm/issues/38)) ([113f970](https://github.com/meridianlabs-ai/inspect_swarm/commit/113f970465d65392ee8ddbbe204ddcc281fb1c69))
* gate checks as per-member metrics ([#47](https://github.com/meridianlabs-ai/inspect_swarm/issues/47)) ([36ff35f](https://github.com/meridianlabs-ai/inspect_swarm/commit/36ff35ff01c308a7eb9557559e5a1232581328c8))
* leaderless stops the other members at the first submission ([#65](https://github.com/meridianlabs-ai/inspect_swarm/issues/65)) ([1fc0a5e](https://github.com/meridianlabs-ai/inspect_swarm/commit/1fc0a5e6f18b475a4697e36b0bc717d75f91b397))
* members can see the swarm budget that will stop them ([#45](https://github.com/meridianlabs-ai/inspect_swarm/issues/45)) ([e80345f](https://github.com/meridianlabs-ai/inspect_swarm/commit/e80345f44e788d71f414715fd39ea6fb4383ff20))
* no store events inside the swarm ([#46](https://github.com/meridianlabs-ai/inspect_swarm/issues/46)) ([657b132](https://github.com/meridianlabs-ai/inspect_swarm/commit/657b1320b51c5a6e117a13abfaa5d49e7ee04cb6))
* notices for swarm members ([#60](https://github.com/meridianlabs-ai/inspect_swarm/issues/60)) ([bc6056e](https://github.com/meridianlabs-ai/inspect_swarm/commit/bc6056e805c5b0176fb844ca12652f1e5c31261a))
* per-member sandboxes ([#48](https://github.com/meridianlabs-ai/inspect_swarm/issues/48)) ([6e5027b](https://github.com/meridianlabs-ai/inspect_swarm/commit/6e5027b85e207478ecdd1ce5dfcd6b49811b2c15))
* per-member timelines, swarm metrics and harness-validity checks ([#41](https://github.com/meridianlabs-ai/inspect_swarm/issues/41)) ([1d1859b](https://github.com/meridianlabs-ai/inspect_swarm/commit/1d1859bb0b93871dcdb7c5b2bfb9762bfa649ada))
* shared_memory append command that needs no prior view ([#69](https://github.com/meridianlabs-ai/inspect_swarm/issues/69)) ([78aaa71](https://github.com/meridianlabs-ai/inspect_swarm/commit/78aaa714b72e4b64668eedb07b97dde9e27242ee))
* steps-to-answer and longest-member-steps metrics for leaderless swarms ([#43](https://github.com/meridianlabs-ai/inspect_swarm/issues/43)) ([f9eeb66](https://github.com/meridianlabs-ai/inspect_swarm/commit/f9eeb66fda5ccc5413037fad039eeae5d5a6c151))
* swarm budget and limit nodes ([#17](https://github.com/meridianlabs-ai/inspect_swarm/issues/17)) ([8959584](https://github.com/meridianlabs-ai/inspect_swarm/commit/89595846d0a901d53b540d4f94c8c425c9fa6227))
* swarm controller and channel registry plumbing ([#19](https://github.com/meridianlabs-ai/inspect_swarm/issues/19)) ([290f562](https://github.com/meridianlabs-ai/inspect_swarm/commit/290f562f543d18754f1e92da6b417b29eef2dff5))
* swarm runtime core: controller surface, members and stopping ([#27](https://github.com/meridianlabs-ai/inspect_swarm/issues/27)) ([e44a671](https://github.com/meridianlabs-ai/inspect_swarm/commit/e44a671055cd263e1c92efde5271fdbf28c74271))
* swarm_wait, waiting for other members ([#62](https://github.com/meridianlabs-ai/inspect_swarm/issues/62)) ([16ae04c](https://github.com/meridianlabs-ai/inspect_swarm/commit/16ae04cd450d8969c338d3d231bf442e01b8b45d))
* SwarmColumns, swarm cost and ledger columns for samples_df() ([#40](https://github.com/meridianlabs-ai/inspect_swarm/issues/40)) ([a5d8f86](https://github.com/meridianlabs-ai/inspect_swarm/commit/a5d8f86c3f22f7b06e9a183f0e4ef320e767ad6a))
* the shared-memory channel ([#49](https://github.com/meridianlabs-ai/inspect_swarm/issues/49)) ([0d3cdf4](https://github.com/meridianlabs-ai/inspect_swarm/commit/0d3cdf4dc1f9b464ef77f0f31d920fb55943c7ec))
* the swarm's communication bus ([#33](https://github.com/meridianlabs-ai/inspect_swarm/issues/33)) ([e00718d](https://github.com/meridianlabs-ai/inspect_swarm/commit/e00718dcf2f0aab82927c770216c8770d2acadea))
* the swarm's result record and the `first` answer ([#31](https://github.com/meridianlabs-ai/inspect_swarm/issues/31)) ([732ccc1](https://github.com/meridianlabs-ai/inspect_swarm/commit/732ccc13cd1f510e8bc810eb022735912e05d727))
* the swarm's usage ledger ([#32](https://github.com/meridianlabs-ai/inspect_swarm/issues/32)) ([0ac8d33](https://github.com/meridianlabs-ai/inspect_swarm/commit/0ac8d33f86dfa6b4550735eedd1051e221b34db4))
