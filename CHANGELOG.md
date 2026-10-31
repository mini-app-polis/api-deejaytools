## [1.20.1](https://github.com/mini-app-polis/api-deejaytools/compare/v1.20.0...v1.20.1) (2026-10-31)


### Bug Fixes

* **middleware:** key the rate limit on an address the client cannot forge ([9a6a80d](https://github.com/mini-app-polis/api-deejaytools/commit/9a6a80d083cc12c7935aa1650984a7e24e70e856))

# [1.20.0](https://github.com/mini-app-polis/api-deejaytools/compare/v1.19.1...v1.20.0) (2026-10-31)


### Bug Fixes

* **admin-users:** keep the per-user submissions list on SubmissionData, without can_withdraw ([1c94e56](https://github.com/mini-app-polis/api-deejaytools/commit/1c94e56e3e5e3c4ad2b3d6c0455740a7ddf9174a))
* **deps:** bump miniapppolis-common-utils to 7.7.0 ([6bd2a64](https://github.com/mini-app-polis/api-deejaytools/commit/6bd2a64b93a73a94b6d789b39435473db3bbd1cd))


### Features

* **event-song-submissions:** list partner-shared songs' submissions with can_withdraw ([281c671](https://github.com/mini-app-polis/api-deejaytools/commit/281c6716c182a6ec0ddbb11b7835d5f2e3f5fe7a))

## [1.19.1](https://github.com/mini-app-polis/api-deejaytools/compare/v1.19.0...v1.19.1) (2026-10-31)


### Bug Fixes

* **deps:** bump the minor-and-patch group with 4 updates ([8608e9f](https://github.com/mini-app-polis/api-deejaytools/commit/8608e9f8234fcf5155a4a585dd3f24cf0d4fa058))

# [1.19.0](https://github.com/mini-app-polis/api-deejaytools/compare/v1.18.1...v1.19.0) (2026-10-31)


### Bug Fixes

* **deps:** bump pyasn1 to 0.6.5 for GHSA-mm57-4v3p-rchx, GHSA-fm9c-5rcf-2q5p, GHSA-9m75-vc3g-4x39 and GHSA-2qm8-x737-gmw5 ([f34233a](https://github.com/mini-app-polis/api-deejaytools/commit/f34233a6aa3ecfec746f45be081e7c11e7beb7de))


### Features

* **staff:** tighten event staff access and fix queue races from the platform review ([a63b1d1](https://github.com/mini-app-polis/api-deejaytools/commit/a63b1d10a37eea688b19d53d121e217745304073))

## [1.18.1](https://github.com/mini-app-polis/api-deejaytools/compare/v1.18.0...v1.18.1) (2026-10-30)


### Bug Fixes

* **drive:** cap each event folder sweep so a backlog never holds up the scheduler ([2fab321](https://github.com/mini-app-polis/api-deejaytools/commit/2fab321ce4458b5388a9725cec6e0122044945c3))

# [1.18.0](https://github.com/mini-app-polis/api-deejaytools/compare/v1.17.0...v1.18.0) (2026-10-30)


### Features

* **checkins:** show a couple's check-ins to the linked partner, hold one entry per couple, let a linked partner find the owner's pair, and sweep deleted events' empty Drive folders ([b42cf5b](https://github.com/mini-app-polis/api-deejaytools/commit/b42cf5b3b8ae33c0e52b58cb69905f746be7c804))

# [1.17.0](https://github.com/mini-app-polis/api-deejaytools/compare/v1.16.2...v1.17.0) (2026-10-30)


### Features

* **songs:** let a song's linked partner read it, submit it to events and check it in, keeping PATCH and DELETE owner-only ([2085c8c](https://github.com/mini-app-polis/api-deejaytools/commit/2085c8c6c0a6c4fdc44bc5a618a1a79163a37a88))

## [1.16.2](https://github.com/mini-app-polis/api-deejaytools/compare/v1.16.1...v1.16.2) (2026-10-30)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 7.2.0 ([6b32c19](https://github.com/mini-app-polis/api-deejaytools/commit/6b32c192e9b3d3f0bc1c8ebd0b4409cbfa243546))
* **deps:** bump miniapppolis-common-utils to 7.3.0 ([7856727](https://github.com/mini-app-polis/api-deejaytools/commit/7856727be4be462e9c752f000904c07210eef14d))
* **deps:** bump miniapppolis-common-utils to 7.4.0 ([692c5a0](https://github.com/mini-app-polis/api-deejaytools/commit/692c5a06feeada062d9905d62365e8a1abcf30aa))
* **deps:** bump miniapppolis-common-utils to 7.5.0 ([734674a](https://github.com/mini-app-polis/api-deejaytools/commit/734674a321bd399553d070352695649ccf0a848d))
* **deps:** bump miniapppolis-common-utils to 7.6.0 ([0f0134e](https://github.com/mini-app-polis/api-deejaytools/commit/0f0134eed44055df5de4fb6f071b10849b540999))

## [1.16.1](https://github.com/mini-app-polis/api-deejaytools/compare/v1.16.0...v1.16.1) (2026-10-29)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 7.1.1 ([9767d4f](https://github.com/mini-app-polis/api-deejaytools/commit/9767d4fdc3339e294f9be44c310548b6ee709287))

# [1.16.0](https://github.com/mini-app-polis/api-deejaytools/compare/v1.15.0...v1.16.0) (2026-10-29)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 7.1.0 ([cf095a3](https://github.com/mini-app-polis/api-deejaytools/commit/cf095a3195cf77540e3f9668a6142ea3e879f107))
* **songs:** share a song's Drive file with a partner set after it was built ([36bb6f1](https://github.com/mini-app-polis/api-deejaytools/commit/36bb6f193ddcb5c9604260968418312795782eb6))


### Features

* **activity:** name people in the change feed by their first and last name ([dcb8538](https://github.com/mini-app-polis/api-deejaytools/commit/dcb85385f138a1d1decb41efc28e66a68873e428))

# [1.15.0](https://github.com/mini-app-polis/api-deejaytools/compare/v1.14.1...v1.15.0) (2026-10-29)


### Bug Fixes

* **uploads:** narrow the submit-to-event id so the type check passes ([0e36b6b](https://github.com/mini-app-polis/api-deejaytools/commit/0e36b6bd4a3c8e8f20a30f23612cac24105a9620))


### Features

* **divisions:** a public GET /v1/divisions, and feedback addresses from settings ([94732c6](https://github.com/mini-app-polis/api-deejaytools/commit/94732c61a9d6e7862858933086333ed460c94cb7))
* **uploads:** a staff upload on behalf names the dancer's partner and submits the song to the event ([8f7eaa2](https://github.com/mini-app-polis/api-deejaytools/commit/8f7eaa20a2450b45136b8c81fffe02e177bdfcfa))

## [1.14.1](https://github.com/mini-app-polis/api-deejaytools/compare/v1.14.0...v1.14.1) (2026-10-29)


### Bug Fixes

* **auth:** pass a literal scope to the drive-folder route guard so AUTH-003 can read it ([85e7304](https://github.com/mini-app-polis/api-deejaytools/commit/85e73041838b64b3d1ab88cab1957a1927174db2))
* **deps:** bump miniapppolis-common-utils to 6.36.0 ([413dde9](https://github.com/mini-app-polis/api-deejaytools/commit/413dde9b732494782a64a76d5af492b601a295ef))
* **deps:** move to common-python-utils 7.x, which posts changes and faults from production only ([81a50b4](https://github.com/mini-app-polis/api-deejaytools/commit/81a50b41449c6cc09bd09928bd15d647977e3d45))

# [1.14.0](https://github.com/mini-app-polis/api-deejaytools/compare/v1.13.2...v1.14.0) (2026-10-29)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.34.0 ([c59af9b](https://github.com/mini-app-polis/api-deejaytools/commit/c59af9b60fbac61550b625608a42814e8b9b6d3e))
* **deps:** bump miniapppolis-common-utils to 6.35.0 ([33a90ff](https://github.com/mini-app-polis/api-deejaytools/commit/33a90ffe9f1420590c57d4c1a1362799d616dcad))


### Features

* **events:** GET /v1/events/{id}/drive-folder returns the event's shared Drive folder ([cb8e39c](https://github.com/mini-app-polis/api-deejaytools/commit/cb8e39c7f2abc72e6c289fd1297632ea23de72ff))
* **submissions:** return owner_user_id on each row of an event's song submissions ([321e0a7](https://github.com/mini-app-polis/api-deejaytools/commit/321e0a77a7760dd8c0fa612a3d1c28447f918014))

## [1.13.2](https://github.com/mini-app-polis/api-deejaytools/compare/v1.13.1...v1.13.2) (2026-10-28)


### Bug Fixes

* **config:** treat an empty setting as unset so its default applies ([a309860](https://github.com/mini-app-polis/api-deejaytools/commit/a309860994aa45dd5456ed17f9607a046df415d5))

## [1.13.1](https://github.com/mini-app-polis/api-deejaytools/compare/v1.13.0...v1.13.1) (2026-10-28)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.33.0 ([3b54546](https://github.com/mini-app-polis/api-deejaytools/commit/3b545460b4aa48da3ed8ff903ec4cdbf85dba150))

# [1.13.0](https://github.com/mini-app-polis/api-deejaytools/compare/v1.12.0...v1.13.0) (2026-10-28)


### Bug Fixes

* **auth:** pass literal scope strings to route guards so AUTH-003 can read them ([19c5114](https://github.com/mini-app-polis/api-deejaytools/commit/19c5114417265c1e6cb6672cb34cbd81f5639361))


### Features

* **events:** find an event's people by name for its staff ([9cd09a1](https://github.com/mini-app-polis/api-deejaytools/commit/9cd09a1c5991464615a895bbb4e40e766c835323))

# [1.12.0](https://github.com/mini-app-polis/api-deejaytools/compare/v1.11.0...v1.12.0) (2026-10-28)


### Features

* **auth:** event-scope events.write on event PATCH and DELETE for event managers ([a353779](https://github.com/mini-app-polis/api-deejaytools/commit/a3537795f5480c416b9a85f7bcd318f99896bdec))
* **auth:** give event staff read access to their event's song submissions ([7258c8a](https://github.com/mini-app-polis/api-deejaytools/commit/7258c8aaafa105fd19f4218a526fc162ddf04672))
* **auth:** give the per-event DJ role its API permissions ([711d404](https://github.com/mini-app-polis/api-deejaytools/commit/711d404dd0efcbb692bdb03c944a48f251f4b574))

# [1.11.0](https://github.com/mini-app-polis/api-deejaytools/compare/v1.10.2...v1.11.0) (2026-10-28)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.32.0 ([0f8f55d](https://github.com/mini-app-polis/api-deejaytools/commit/0f8f55d3dc89601f5b988eb16d60f16dfb0e4758))


### Features

* **auth:** per-event staff roles with event-scoped authorization and grant endpoints ([0399e1e](https://github.com/mini-app-polis/api-deejaytools/commit/0399e1ee0d8bf853c56cfa69f2cb31cc5ecd76d3))

## [1.10.2](https://github.com/mini-app-polis/api-deejaytools/compare/v1.10.1...v1.10.2) (2026-10-28)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.31.0 ([a681e90](https://github.com/mini-app-polis/api-deejaytools/commit/a681e90fae55bbd0f43779236ea2bdedf98c36c5))

## [1.10.1](https://github.com/mini-app-polis/api-deejaytools/compare/v1.10.0...v1.10.1) (2026-10-27)


### Bug Fixes

* **railway:** drop PORT and set optional settings through Doppler ([c32f769](https://github.com/mini-app-polis/api-deejaytools/commit/c32f76909ab445fdbedc72640977b638191914ed))
* **railway:** keep PORT, which Railway otherwise replaces with its own ([6a93d8a](https://github.com/mini-app-polis/api-deejaytools/commit/6a93d8a473759bfae8d4d8ddb2805a0fe4c2ff50))

# [1.10.0](https://github.com/mini-app-polis/api-deejaytools/compare/v1.9.6...v1.10.0) (2026-10-27)


### Features

* **railway:** keep the variables Doppler syncs to the service itself ([a54847c](https://github.com/mini-app-polis/api-deejaytools/commit/a54847c816b688f8fe31756db7ebde9516cae9e1))

## [1.9.6](https://github.com/mini-app-polis/api-deejaytools/compare/v1.9.5...v1.9.6) (2026-10-26)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.28.0 ([5985a44](https://github.com/mini-app-polis/api-deejaytools/commit/5985a44b385275664adf3dfdf0bf46d0aea813d6))
* **deps:** bump miniapppolis-common-utils to 6.29.0 ([e6685b2](https://github.com/mini-app-polis/api-deejaytools/commit/e6685b2c6f23bb0f0d1f8270a469bcc42859a003))
* **deps:** bump miniapppolis-common-utils to 6.30.0 ([3341390](https://github.com/mini-app-polis/api-deejaytools/commit/3341390c8f54909cd4595d716d1b4e307ae126a6))

## [1.9.5](https://github.com/mini-app-polis/api-deejaytools/compare/v1.9.4...v1.9.5) (2026-10-26)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.27.0 ([4e614b7](https://github.com/mini-app-polis/api-deejaytools/commit/4e614b75e8cb5794804e031e727a5a5f76d32c14))

## [1.9.4](https://github.com/mini-app-polis/api-deejaytools/compare/v1.9.3...v1.9.4) (2026-10-25)


### Bug Fixes

* **deps:** upgrade miniapppolis-identity to 2.3.0 and add identity_audit_events.key_slot ([06ab7d9](https://github.com/mini-app-polis/api-deejaytools/commit/06ab7d91292f70977b6837504679134ba5d05bba))

## [1.9.3](https://github.com/mini-app-polis/api-deejaytools/compare/v1.9.2...v1.9.3) (2026-10-25)


### Bug Fixes

* **deps:** bump the minor-and-patch group with 7 updates ([9e3ccb5](https://github.com/mini-app-polis/api-deejaytools/commit/9e3ccb533bd82ea466e720c0f7afd84551b73063))

## [1.9.2](https://github.com/mini-app-polis/api-deejaytools/compare/v1.9.1...v1.9.2) (2026-10-25)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.26.0 ([f15b3c5](https://github.com/mini-app-polis/api-deejaytools/commit/f15b3c58a50ed86ce1828c95e70b471d8061f989))

## [1.9.1](https://github.com/mini-app-polis/api-deejaytools/compare/v1.9.0...v1.9.1) (2026-10-25)


### Bug Fixes

* **api:** separate dancer submission label pieces with dashes ([e6262fc](https://github.com/mini-app-polis/api-deejaytools/commit/e6262fc6c46574e4defcd4ae930b1bcb2e9e9c1f))
* **deps:** bump miniapppolis-common-utils to 6.25.0 ([90ff689](https://github.com/mini-app-polis/api-deejaytools/commit/90ff6896285e50ae17da77861bb5d682d4926b4e))

# [1.9.0](https://github.com/mini-app-polis/api-deejaytools/compare/v1.8.3...v1.9.0) (2026-10-25)


### Bug Fixes

* **api:** drop the season year from dancer submission labels ([b22d669](https://github.com/mini-app-polis/api-deejaytools/commit/b22d6695e40df924f4095300c35e0d6885d10dfc))
* **deps:** bump miniapppolis-common-utils to 6.24.0 ([49a7d07](https://github.com/mini-app-polis/api-deejaytools/commit/49a7d07b2865fffb01ef4621a5a304717998b789))


### Features

* **internal:** accept TICK_SECRET_PREVIOUS on x-tick-secret during rotation overlap ([c2fc1c7](https://github.com/mini-app-polis/api-deejaytools/commit/c2fc1c796e672ee6738e0ac24a6057d99db0911b))

## [1.8.3](https://github.com/mini-app-polis/api-deejaytools/compare/v1.8.2...v1.8.3) (2026-10-25)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.23.0 ([056ad01](https://github.com/mini-app-polis/api-deejaytools/commit/056ad01bb521f7ab768cae8e2aa26de8bfa49eb6))

## [1.8.2](https://github.com/mini-app-polis/api-deejaytools/compare/v1.8.1...v1.8.2) (2026-10-25)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.22.3 ([53ed371](https://github.com/mini-app-polis/api-deejaytools/commit/53ed371c79e10603795e066d2d79df543e550b24))

## [1.8.1](https://github.com/mini-app-polis/api-deejaytools/compare/v1.8.0...v1.8.1) (2026-10-25)


### Bug Fixes

* **deps:** bump miniapppolis-identity to 2.1.10 ([cf2a4db](https://github.com/mini-app-polis/api-deejaytools/commit/cf2a4db80b8e25e00e4f43c61d17c0a9320a416b))

# [1.8.0](https://github.com/mini-app-polis/api-deejaytools/compare/v1.7.0...v1.8.0) (2026-10-25)


### Features

* **notifications:** add a per-environment Discord event allow-list and drop data-changed messages in development ([b78ede5](https://github.com/mini-app-polis/api-deejaytools/commit/b78ede51bfad5468a81c8e0ffaea743c79432336))

# [1.7.0](https://github.com/mini-app-polis/api-deejaytools/compare/v1.6.2...v1.7.0) (2026-10-24)


### Features

* **auth:** drop users.role and decide admin from identity grants only ([c46ed5e](https://github.com/mini-app-polis/api-deejaytools/commit/c46ed5e403f5382a4f37a79104b055783b0f4445))

## [1.6.2](https://github.com/mini-app-polis/api-deejaytools/compare/v1.6.1...v1.6.2) (2026-10-24)


### Bug Fixes

* **admin:** the admin Songs label leads with the leader ([1e70a9c](https://github.com/mini-app-polis/api-deejaytools/commit/1e70a9cca87cc006d9759f02ad6460998862effc))
* **deps:** bump miniapppolis-common-utils to 6.22.1 ([a15e5db](https://github.com/mini-app-polis/api-deejaytools/commit/a15e5dbd29e7c6a3adfd85ee68807720fcf0f84f))
* **deps:** bump miniapppolis-common-utils to 6.22.2 ([9772f76](https://github.com/mini-app-polis/api-deejaytools/commit/9772f76e2920f7140eaeeff90afe206cfc7748f0))
* **deps:** bump miniapppolis-identity to 2.1.9 ([40011a4](https://github.com/mini-app-polis/api-deejaytools/commit/40011a4b8ea23642aa2b6939e4b0ce6e237f2771))
* **queue:** queue, check-in and run entity labels lead with the leader ([ea04eae](https://github.com/mini-app-polis/api-deejaytools/commit/ea04eae103420d9fd6cb27a813f474337b87336b))

## [1.6.1](https://github.com/mini-app-polis/api-deejaytools/compare/v1.6.0...v1.6.1) (2026-10-23)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.21.0 ([f005942](https://github.com/mini-app-polis/api-deejaytools/commit/f005942efa1d83af61d776def391dced172091b4))
* **deps:** bump miniapppolis-common-utils to 6.22.0 ([a6c8748](https://github.com/mini-app-polis/api-deejaytools/commit/a6c8748f5417e990b061b09e1034e8005c15b4ec))
* **deps:** patch vulnerable dependencies (filelock) ([aa88847](https://github.com/mini-app-polis/api-deejaytools/commit/aa888472beb7cddd3a8051f6b8c023c02a046a1f))
* **deps:** patch vulnerable dependencies (virtualenv) ([be8ade3](https://github.com/mini-app-polis/api-deejaytools/commit/be8ade3cf81edc5ff2fd66fda0ce0269503e6e37))

# [1.6.0](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.33...v1.6.0) (2026-10-23)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.18.0 ([ee14fca](https://github.com/mini-app-polis/api-deejaytools/commit/ee14fca8df3ef57d3bc36f3511126408809af567))
* **deps:** bump miniapppolis-common-utils to 6.20.0 ([54037c9](https://github.com/mini-app-polis/api-deejaytools/commit/54037c9886f467366410a2515c1f4c8d472e63f5))


### Features

* **songs:** return a ready-made partnership_label on song reads ([8731575](https://github.com/mini-app-polis/api-deejaytools/commit/8731575a96fa030dc1f725a20f5eb40afba51881))

## [1.5.33](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.32...v1.5.33) (2026-10-23)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.16.2 ([052dbc3](https://github.com/mini-app-polis/api-deejaytools/commit/052dbc343442d78a7b4eba77cf72c0f1f4a46a5e))
* **deps:** bump miniapppolis-common-utils to 6.17.0 ([3fb4627](https://github.com/mini-app-polis/api-deejaytools/commit/3fb462756d490327e3be209cb57a46c0a640f6ff))

## [1.5.32](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.31...v1.5.32) (2026-10-22)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.16.1 ([db6c75b](https://github.com/mini-app-polis/api-deejaytools/commit/db6c75baa27990f2d4874d226e17970c7e158bac))

## [1.5.31](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.30...v1.5.31) (2026-10-22)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.16.0 ([4dced7b](https://github.com/mini-app-polis/api-deejaytools/commit/4dced7b7d7f54b1a97d74e365d3c725740bbdbd3))

## [1.5.30](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.29...v1.5.30) (2026-10-22)


### Bug Fixes

* **config:** one Google key file and one Brevo key ([ced5519](https://github.com/mini-app-polis/api-deejaytools/commit/ced551917d44ae3cf725e0b99418d0dc08830057))
* **deps:** patch vulnerable dependencies (opentelemetry-api) ([7c96b00](https://github.com/mini-app-polis/api-deejaytools/commit/7c96b0065ade2d4059898de938ee77c97537662f))

## [1.5.29](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.28...v1.5.29) (2026-10-21)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.15.1 ([e888777](https://github.com/mini-app-polis/api-deejaytools/commit/e88877740bd1339736c1ea06aff635cc0eb6f9c9))
* **deps:** bump miniapppolis-identity to 2.1.8 ([bf6f8ef](https://github.com/mini-app-polis/api-deejaytools/commit/bf6f8efbe65c01c7f6109ba99021ce2592328e93))

## [1.5.28](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.27...v1.5.28) (2026-10-21)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.15.0 ([5d4e576](https://github.com/mini-app-polis/api-deejaytools/commit/5d4e57655b5bc5efe2f6f0b1de2fb8fb6b878962))
* **deps:** patch vulnerable dependencies (virtualenv) ([cb42f29](https://github.com/mini-app-polis/api-deejaytools/commit/cb42f29b8caa3a9050e4a66bad626eb0d53de1cd))

## [1.5.27](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.26...v1.5.27) (2026-10-21)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.14.0 ([c6bb907](https://github.com/mini-app-polis/api-deejaytools/commit/c6bb90748d72fce2ecc0a1f8247e1672eda1b1b5))

## [1.5.26](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.25...v1.5.26) (2026-10-21)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.12.0 ([eb352c8](https://github.com/mini-app-polis/api-deejaytools/commit/eb352c8cce808b189a1623581871712d866690cc))
* **deps:** bump miniapppolis-common-utils to 6.13.0 ([ebf109e](https://github.com/mini-app-polis/api-deejaytools/commit/ebf109e53f34e3c02a5b0c428d8b4c14b2586314))
* **sessions:** clear the cached session reads when a session is created or its event changes ([d50bb42](https://github.com/mini-app-polis/api-deejaytools/commit/d50bb429b709bb9f8d5518db3bc29e25cc8a4b28))

## [1.5.25](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.24...v1.5.25) (2026-10-21)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.11.2 ([fe10217](https://github.com/mini-app-polis/api-deejaytools/commit/fe10217b151f2514e29bd16ff94f39d89d086d2d))
* **deps:** bump miniapppolis-identity to 2.1.7 ([ce5205d](https://github.com/mini-app-polis/api-deejaytools/commit/ce5205de32edc93fcf17fa76b3750820d6ccfd56))

## [1.5.24](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.23...v1.5.24) (2026-10-21)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.11.1 ([628350e](https://github.com/mini-app-polis/api-deejaytools/commit/628350e7df4e06f8288df7af018e773d1f5d89c4))

## [1.5.23](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.22...v1.5.23) (2026-10-21)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.11.0 ([348de7e](https://github.com/mini-app-polis/api-deejaytools/commit/348de7e4b4a103aa3227de09589b249bad6874e3))
* **deps:** patch vulnerable dependencies (virtualenv) ([0833edf](https://github.com/mini-app-polis/api-deejaytools/commit/0833edfb02e0446e46c7bbabaf17b104dc3640ce))

## [1.5.22](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.21...v1.5.22) (2026-10-20)


### Bug Fixes

* **auth:** answer 503 instead of 401 when the issuer is unreachable ([cf21e74](https://github.com/mini-app-polis/api-deejaytools/commit/cf21e7476608034c5512c0b84f55787da16f2dd6))

## [1.5.21](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.20...v1.5.21) (2026-10-20)


### Bug Fixes

* **labels:** name song partnerships leader first, by team, and ProAm am/pro in submission and run labels ([92b1039](https://github.com/mini-app-polis/api-deejaytools/commit/92b10399e9f8e204bb0ceee4c714f7d47ef16dbe))

## [1.5.20](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.19...v1.5.20) (2026-10-20)


### Bug Fixes

* **deps:** bump miniapppolis-identity to 2.1.5 ([42c4209](https://github.com/mini-app-polis/api-deejaytools/commit/42c4209072d5cd0cc8cdc1b1e65405a495e973ce))

## [1.5.19](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.18...v1.5.19) (2026-10-20)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.10.0 ([b8daf5a](https://github.com/mini-app-polis/api-deejaytools/commit/b8daf5a52623fa438fd13b4f962f834ce77ddae7))

## [1.5.18](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.17...v1.5.18) (2026-10-20)


### Bug Fixes

* **auth:** stop users.role overriding admin grants on deploy ([f3f536d](https://github.com/mini-app-polis/api-deejaytools/commit/f3f536d5ea25c12f5dfdf7d8af45545a9e3080cd))
* **auth:** stop users.role overriding admin grants on deploy ([e92c158](https://github.com/mini-app-polis/api-deejaytools/commit/e92c158e1ac4686c8f9964ff823658971b39e230))
* **deps:** bump certifi to 2026.10.19 for GHSA-6q48-6c7w-9gw2 ([67b0505](https://github.com/mini-app-polis/api-deejaytools/commit/67b05051a0cceb9f25f8d41dc8cc72e89104a297))

## [1.5.17](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.16...v1.5.17) (2026-10-20)


### Bug Fixes

* **events:** label entered entities by leader/follower, team name, and ProAm am/pro order ([21c8b6d](https://github.com/mini-app-polis/api-deejaytools/commit/21c8b6db96a6f69790b8dd91468a994aa55fcae2))

## [1.5.16](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.15...v1.5.16) (2026-10-19)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.9.0 ([bd4d01d](https://github.com/mini-app-polis/api-deejaytools/commit/bd4d01d9267e4dabcdf34ebbc0cb28ce1c9e0bfd))

## [1.5.15](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.14...v1.5.15) (2026-10-19)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.7.0 ([19dda01](https://github.com/mini-app-polis/api-deejaytools/commit/19dda011dab394e1de4fd16f406e608f4e4af773))
* **deps:** bump miniapppolis-common-utils to 6.7.1 ([20b067a](https://github.com/mini-app-polis/api-deejaytools/commit/20b067ae52721ec809247ba19c138096439df324))
* **deps:** bump miniapppolis-common-utils to 6.8.0 ([6cc3d33](https://github.com/mini-app-polis/api-deejaytools/commit/6cc3d333ed3c5aeea7e7a59a746a9e7700c530a4))

## [1.5.14](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.13...v1.5.14) (2026-10-18)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.6.0 ([f4dfdb9](https://github.com/mini-app-polis/api-deejaytools/commit/f4dfdb9684ab2ccc0ff9e08393e5b3b07d1481fd))

## [1.5.13](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.12...v1.5.13) (2026-10-18)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.5.0 ([0c689e7](https://github.com/mini-app-polis/api-deejaytools/commit/0c689e72a4156707a80cbd6af7b975829c19a40d))

## [1.5.12](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.11...v1.5.12) (2026-10-17)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.4.0 ([e08d077](https://github.com/mini-app-polis/api-deejaytools/commit/e08d07753f486f5e41b14ab3ff25f3a54b6519a0))

## [1.5.11](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.10...v1.5.11) (2026-10-17)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.3.0 ([fcfb089](https://github.com/mini-app-polis/api-deejaytools/commit/fcfb089d18ceae5607bd91b94c6b174a27a41ee5))

## [1.5.10](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.9...v1.5.10) (2026-10-17)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.2.0 ([8c168eb](https://github.com/mini-app-polis/api-deejaytools/commit/8c168eb49866c7a3654162b8970e343eb271e888))

## [1.5.9](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.8...v1.5.9) (2026-10-16)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 6.1.0 ([0ed524e](https://github.com/mini-app-polis/api-deejaytools/commit/0ed524edf5e7dfe467c58a3a97ca23c38ebfd128))
* **deps:** bump miniapppolis-common-utils to 6.1.1 ([d76047c](https://github.com/mini-app-polis/api-deejaytools/commit/d76047c23c51df7c211ae5577102e0da6a09c528))

## [1.5.8](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.7...v1.5.8) (2026-10-16)


### Bug Fixes

* **deps:** require miniapppolis-common-utils 6 (>=6.0.1) ([e109df1](https://github.com/mini-app-polis/api-deejaytools/commit/e109df18421dd11ed2620b27d2f06bda8d8c130f))

## [1.5.7](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.6...v1.5.7) (2026-10-16)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 5.25.5 ([ff2123f](https://github.com/mini-app-polis/api-deejaytools/commit/ff2123f7a55f17b135c00c7a41f0b5576a4eef07))

## [1.5.6](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.5...v1.5.6) (2026-10-16)


### Bug Fixes

* **build:** start the API from the image's CMD; Railway doesn't apply railway.json to this service ([115b9ba](https://github.com/mini-app-polis/api-deejaytools/commit/115b9bab35c05e096c86e47568964c3b367d2501))

## [1.5.5](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.4...v1.5.5) (2026-10-15)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 5.25.4 ([a1b84e0](https://github.com/mini-app-polis/api-deejaytools/commit/a1b84e0beacd032c76e6dd8818f72b5d4bf3dfd1))

## [1.5.4](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.3...v1.5.4) (2026-10-12)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 5.25.3 ([a2439de](https://github.com/mini-app-polis/api-deejaytools/commit/a2439de6dc26e390a947d90fe715f3a7ffcfd109))

## [1.5.3](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.2...v1.5.3) (2026-10-12)


### Bug Fixes

* **deps:** bump the minor-and-patch group with 2 updates ([cdda0b7](https://github.com/mini-app-polis/api-deejaytools/commit/cdda0b7f0a6ec2ab217c466b1d91ed53a8cef9ec))

## [1.5.2](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.1...v1.5.2) (2026-10-11)


### Bug Fixes

* **deps:** bump miniapppolis-identity to 2.1.4 ([e6847f5](https://github.com/mini-app-polis/api-deejaytools/commit/e6847f5c3d1f3bff8901e69332e7febda99d58f2))

## [1.5.1](https://github.com/mini-app-polis/api-deejaytools/compare/v1.5.0...v1.5.1) (2026-10-11)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 5.25.2 ([175c135](https://github.com/mini-app-polis/api-deejaytools/commit/175c1353092e164f2e07e14ab62e8d5b959b007c))

# [1.5.0](https://github.com/mini-app-polis/api-deejaytools/compare/v1.4.2...v1.5.0) (2026-10-10)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 5.25.1 ([be229c0](https://github.com/mini-app-polis/api-deejaytools/commit/be229c0e6cf3011fa87a115301743925aa11a50b))


### Features

* **observability:** per-route latency for the event-floor routes ([5641d42](https://github.com/mini-app-polis/api-deejaytools/commit/5641d4216fda9a5736589ad5bdba945b58dc0728))

## [1.4.2](https://github.com/mini-app-polis/api-deejaytools/compare/v1.4.1...v1.4.2) (2026-10-09)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 5.25.0 ([45ae74c](https://github.com/mini-app-polis/api-deejaytools/commit/45ae74cc946fef890241ef0fede6e08281cfe6fe))

## [1.4.1](https://github.com/mini-app-polis/api-deejaytools/compare/v1.4.0...v1.4.1) (2026-10-09)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils to 5.24.1 ([283f5d6](https://github.com/mini-app-polis/api-deejaytools/commit/283f5d6cabcf42757357fd3c458f781ed03de639))

# [1.4.0](https://github.com/mini-app-polis/api-deejaytools/compare/v1.3.2...v1.4.0) (2026-10-09)


### Features

* **notifications:** add NOTIFY_DATA_CHANGES, Sentry ids on song and session faults, mute test check-ins ([d970ed1](https://github.com/mini-app-polis/api-deejaytools/commit/d970ed14723206344da4538e5db16ced01ae85ff))
* **notifications:** announce site feedback, keep the live floor out of the change feed ([7172c7a](https://github.com/mini-app-polis/api-deejaytools/commit/7172c7a0a2189eef07700eefbc219a23b4ca98b4))

## [1.3.2](https://github.com/mini-app-polis/api-deejaytools/compare/v1.3.1...v1.3.2) (2026-10-08)


### Bug Fixes

* **observability:** read Sentry DSN from SENTRY_DSN_APIS and tag service ([7d98391](https://github.com/mini-app-polis/api-deejaytools/commit/7d98391760b7001ce398384e0aba6e2ab9cac280))

## [1.3.1](https://github.com/mini-app-polis/api-deejaytools/compare/v1.3.0...v1.3.1) (2026-10-08)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils ([cc5ef5c](https://github.com/mini-app-polis/api-deejaytools/commit/cc5ef5ca50c5b4290eb96a8453d0bdc6ce2bde69))

# [1.3.0](https://github.com/mini-app-polis/api-deejaytools/compare/v1.2.2...v1.3.0) (2026-10-07)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils ([a55dbe8](https://github.com/mini-app-polis/api-deejaytools/commit/a55dbe84b1e6fb46d8eac0d48cf37680bc824510))


### Features

* Discord notifications for songs added, data changes and server errors ([eb75b48](https://github.com/mini-app-polis/api-deejaytools/commit/eb75b485003c4a562b63adcd786c45f15f0f8840))

## [1.2.2](https://github.com/mini-app-polis/api-deejaytools/compare/v1.2.1...v1.2.2) (2026-10-07)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils ([5c25989](https://github.com/mini-app-polis/api-deejaytools/commit/5c259894a7db0d1a4059e4a553f8a34df304d9c3))

## [1.2.1](https://github.com/mini-app-polis/api-deejaytools/compare/v1.2.0...v1.2.1) (2026-10-06)


### Bug Fixes

* **deps:** bump miniapppolis-common-utils ([9be2082](https://github.com/mini-app-polis/api-deejaytools/commit/9be2082292e29d70677973d5358d2fd0a4842369))

# [1.2.0](https://github.com/mini-app-polis/api-deejaytools/compare/v1.1.4...v1.2.0) (2026-10-03)


### Features

* redirect / to the interactive API docs ([760a94b](https://github.com/mini-app-polis/api-deejaytools/commit/760a94befabb899e9807351e2b9bc20c6b0cd342))

## [1.1.4](https://github.com/mini-app-polis/api-deejaytools/compare/v1.1.3...v1.1.4) (2026-10-03)


### Bug Fixes

* **metrics:** publish request metrics with the shared API credentials ([785c137](https://github.com/mini-app-polis/api-deejaytools/commit/785c137f1b9a05738d287f8ae1d59564aadd31be))

## [1.1.3](https://github.com/mini-app-polis/api-deejaytools/compare/v1.1.2...v1.1.3) (2026-10-02)


### Bug Fixes

* deploy run on command ([8a5c0e5](https://github.com/mini-app-polis/api-deejaytools/commit/8a5c0e5094a6d985829ed4dfb8db28b0991be945))
* **sessions,events:** check the token before validating the body ([0103ca5](https://github.com/mini-app-polis/api-deejaytools/commit/0103ca54edfd542ff45206245337566258e38633))

## [1.1.2](https://github.com/mini-app-polis/api-deejaytools/compare/v1.1.1...v1.1.2) (2026-10-02)


### Bug Fixes

* resolve evaluator findings (literal scopes, public routes, docs, ADR) ([3679d20](https://github.com/mini-app-polis/api-deejaytools/commit/3679d20ced1b046bbf9f2d9154131bf7527efb72))

## [1.1.1](https://github.com/mini-app-polis/api-deejaytools/compare/v1.1.0...v1.1.1) (2026-10-02)


### Bug Fixes

* **scheduler:** leave Drive work queued when Drive is not configured ([812b280](https://github.com/mini-app-polis/api-deejaytools/commit/812b280b630af7d3bd44f1129f3d5ddaa4cd7260))

# [1.1.0](https://github.com/mini-app-polis/api-deejaytools/compare/v1.0.0...v1.1.0) (2026-10-02)


### Bug Fixes

* uv lock ([9dbdf15](https://github.com/mini-app-polis/api-deejaytools/commit/9dbdf153a2a9c55c1283d8b93927ce7943d0eec6))


### Features

* add GET /version for the post-deploy smoke test, add promote, automerge and post-deploy smoke workflows ([cd542ec](https://github.com/mini-app-polis/api-deejaytools/commit/cd542ec496ba2f58d5315e255f711efcc25b0ec0))
* conformance suite, request limits, events and sessions routes ([57e70c5](https://github.com/mini-app-polis/api-deejaytools/commit/57e70c50cf339128c8d74a5887ed2afa992345f1))
* port the remaining deejaytools-api routes ([5185897](https://github.com/mini-app-polis/api-deejaytools/commit/5185897f920fe6a30ef43e6357264b933ad3c97d))

# 1.0.0 (2026-10-02)


### Features

* scaffold FastAPI replacement for deejaytools-api ([433bfbc](https://github.com/mini-app-polis/api-deejaytools/commit/433bfbc252f61364063333cf06fe06d1a730c3d5))
