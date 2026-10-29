# Writing output format / source traceability signals

These are deterministic format and explicit-source-traceability signals only.
They are not factual correctness, usable rate, human review or a quality/release gate.
No semantic error labels or editing times are inferred.

Algorithm: writing-output-signals-v1

- Fixed denominator: 100 tasks and 200 planned drafts; 100 requests per arm.
- Include titles. Remove URL tokens, exact known source IDs and S-ID regex tokens,
  line-start Markdown list markers/rules, the published Markdown symbol set, and Unicode whitespace.
- Count remaining Python Unicode code points (not words); 250 to 350 inclusive.
- Exact source IDs/full URLs only; an institution name is not an explicit match.
- Unknown S-IDs take precedence over valid; matching a source does not establish factual support.
- Failed partial drafts are labelled; absent/unknown drafts have unobserved text signals.
- A retained failed or unknown request never disappears from the planned denominator.

Full regexes and step order are recorded in audit.json. Freeze the algorithm and tool SHA before reading evaluation drafts.

| Arm | Planned | Completed | Failed | Unknown | Not run |
| --- | ---: | ---: | ---: | ---: | ---: |
| baseline | 100 | 100 | 0 | 0 | 0 |
| candidate | 100 | 100 | 0 | 0 | 0 |

| Arm | Signal | True | False | Unobserved | Planned |
| --- | --- | ---: | ---: | ---: | ---: |
| baseline | terminal | 100 | 0 | 0 | 100 |
| baseline | finish_stop | 100 | 0 | 0 | 100 |
| baseline | explicit_known_source_match | 5 | 95 | 0 | 100 |
| baseline | residual_ref_marker | 2 | 98 | 0 | 100 |
| baseline | length_within_250_350 | 4 | 96 | 0 | 100 |
| candidate | terminal | 100 | 0 | 0 | 100 |
| candidate | finish_stop | 100 | 0 | 0 | 100 |
| candidate | explicit_known_source_match | 0 | 100 | 0 | 100 |
| candidate | residual_ref_marker | 0 | 100 | 0 | 100 |
| candidate | length_within_250_350 | 8 | 92 | 0 | 100 |

| Arm | Citation status | Count / planned 100 |
| --- | --- | ---: |
| baseline | valid | 5 |
| baseline | missing | 95 |
| baseline | unknown_source_id | 0 |
| baseline | unobserved | 0 |
| candidate | valid | 0 |
| candidate | missing | 100 |
| candidate | unknown_source_id | 0 |
| candidate | unobserved | 0 |

Per-request original artifact SHA and signals:

| Case | Arm | State | Draft kind | Original SHA256 | Finish | Citation | Characters | In range | Residual ref |
| --- | --- | --- | --- | --- | --- | --- | ---: | --- | --- |
| WS-f1d66dbc55d40a45-fact_brief | baseline | completed | completed_draft | 82e09e84ffa44b490d9875da00bc9279d872bbee42c21b57ecbd1e952f697399 | stop | missing | 482 | False | False |
| WS-f1d66dbc55d40a45-fact_brief | candidate | completed | completed_draft | 3f3c1101c05b077b6a8f1b3d8e516555d6fa4db8f4c5f8e23c6761ae9f6561a2 | stop | missing | 486 | False | False |
| WS-f1d66dbc55d40a45-evidence_progress | baseline | completed | completed_draft | ff926396c739af62d95b2fd56c2a092c333704dff96cdca11ceb76702ef86d7e | stop | missing | 547 | False | False |
| WS-f1d66dbc55d40a45-evidence_progress | candidate | completed | completed_draft | e7115abcb3642fdd41b8798391169c8be47ceef9f06f2184b4fa44d2e639ef09 | stop | missing | 472 | False | False |
| WS-6ea189bed54399cf-fact_brief | baseline | completed | completed_draft | eb80470cae03f56215ce42597c8aa00c4a7a821ac3ec36f7ea261c411d0d58d9 | stop | missing | 388 | False | False |
| WS-6ea189bed54399cf-fact_brief | candidate | completed | completed_draft | 2f1af781fc6aa79f92adcabeb21699919e0c77b086d3e31656e453e73e4f675a | stop | missing | 423 | False | False |
| WS-6ea189bed54399cf-evidence_progress | baseline | completed | completed_draft | bbaab83cbc948db022801812e2a69ddae18281a23753c979ae0970692c398146 | stop | missing | 474 | False | False |
| WS-6ea189bed54399cf-evidence_progress | candidate | completed | completed_draft | 22d67eedad8681e3412443d83d15e96e641907d1734ec9762d8a406c043f01f9 | stop | missing | 510 | False | False |
| WS-1e03b9be0410b985-fact_brief | baseline | completed | completed_draft | 1089891f1e86516e55400098b19e7842735e6413dcf44e7c42742ed483420149 | stop | valid | 414 | False | False |
| WS-1e03b9be0410b985-fact_brief | candidate | completed | completed_draft | e2bfaf4be345e18ae82dffd16c6bc9159305d01f3dff4e716d5097f786280f15 | stop | missing | 363 | False | False |
| WS-1e03b9be0410b985-evidence_progress | baseline | completed | completed_draft | 3e35958c0251ad520b4c81236c0ea6418a08bb173895d1245afb2e0c660043ed | stop | valid | 386 | False | False |
| WS-1e03b9be0410b985-evidence_progress | candidate | completed | completed_draft | 23008d9579aee71e5a62c482c89f0e4eb47ba53cd283380badedfa55389fb179 | stop | missing | 352 | False | False |
| WS-1fcb2db58f52f6b4-fact_brief | baseline | completed | completed_draft | fe24d3f681407730f4b3089e85b9afd9813f16fd3a9b6da0692a8fe2dc611ecf | stop | missing | 720 | False | False |
| WS-1fcb2db58f52f6b4-fact_brief | candidate | completed | completed_draft | 968f5095ef16d6093edf57b60f900f4eb99d2024a5fa5f203397190976122992 | stop | missing | 551 | False | False |
| WS-1fcb2db58f52f6b4-evidence_progress | baseline | completed | completed_draft | 801ba464f4b3ccec23f7d6e7fa6dcf7490d819423375b6aa8877d8ab49956dcb | stop | missing | 548 | False | False |
| WS-1fcb2db58f52f6b4-evidence_progress | candidate | completed | completed_draft | 09180e959374bfe0b32879f2d3528c96eb60020266f5b8fc8c69f45964628977 | stop | missing | 423 | False | False |
| WS-fde1595daf994e50-fact_brief | baseline | completed | completed_draft | e402dd391d673bbc7937bf42e4697e94e082a91577748af6d70250219398d1a1 | stop | missing | 423 | False | False |
| WS-fde1595daf994e50-fact_brief | candidate | completed | completed_draft | d01e0da2a5903e0d11f801ba4f287017983a9091452ae4f7fbc07eb48901af17 | stop | missing | 441 | False | False |
| WS-fde1595daf994e50-evidence_progress | baseline | completed | completed_draft | a7c04905c0f443693add09f12fc48a3051cc2d3c0a228d59e76aabfe0063c627 | stop | missing | 455 | False | False |
| WS-fde1595daf994e50-evidence_progress | candidate | completed | completed_draft | f6d0a52693ff046cff30df26b4a2d546fdc320398e4ec517ecf54e028c93d447 | stop | missing | 455 | False | False |
| WS-5344d6f13f6d90cd-fact_brief | baseline | completed | completed_draft | 6d2dc0010d1cfd6e4a864a0d90295fc56338b5ffbe82b8678aac74e270d3872d | stop | missing | 406 | False | False |
| WS-5344d6f13f6d90cd-fact_brief | candidate | completed | completed_draft | 42033484ff4305bf8895714c9fbb863157c1b107018c305c6cf44ec4acf352e8 | stop | missing | 379 | False | False |
| WS-5344d6f13f6d90cd-evidence_progress | baseline | completed | completed_draft | 7d21428ec6c6a8b704930ced34f993c20d15c265d3f0f2afb064a8b4667597e5 | stop | missing | 451 | False | False |
| WS-5344d6f13f6d90cd-evidence_progress | candidate | completed | completed_draft | 3f9e7c885e46d81a02837c8bd7ccf68ceaa0167e68bcc170e23d0e03fe5677a5 | stop | missing | 317 | True | False |
| WS-430afabaebd21126-fact_brief | baseline | completed | completed_draft | 166667f78ab3e8836cc5c52a409cb830310dddd7cddcd83edba6d76cf5b9b9ee | stop | missing | 409 | False | False |
| WS-430afabaebd21126-fact_brief | candidate | completed | completed_draft | 315c52e8c6924f084cf69448526c89f9b396c63b94c0192c9ae5ca8dc872d891 | stop | missing | 424 | False | False |
| WS-430afabaebd21126-evidence_progress | baseline | completed | completed_draft | ca290014a08766e25eb7e38c003418138c5ab80c65280e3c969754661d2696c3 | stop | missing | 561 | False | False |
| WS-430afabaebd21126-evidence_progress | candidate | completed | completed_draft | 444d12d1ffbd35d1f84c40fefc9076fdf22bccff5c58949eb0c6e862080d193e | stop | missing | 376 | False | False |
| WS-f965f8ca9ec9baa2-fact_brief | baseline | completed | completed_draft | c1d0f5364d4128e2751ae1e6fb56257ce427fb6e2c3cca66bcde3bad967e557b | stop | missing | 393 | False | False |
| WS-f965f8ca9ec9baa2-fact_brief | candidate | completed | completed_draft | 372bf7be4413af38c453acb111e5112e84e69c75896064acd76148d86c92a957 | stop | missing | 619 | False | False |
| WS-f965f8ca9ec9baa2-evidence_progress | baseline | completed | completed_draft | f1621adb6e126727690a3f353dd179aed28cf120a9391b5749c6683fc8679168 | stop | missing | 440 | False | False |
| WS-f965f8ca9ec9baa2-evidence_progress | candidate | completed | completed_draft | f43d20d44193b9dfc5f76ecbd0d26c5876d8df2105a88f94fc8415461cf13e00 | stop | missing | 469 | False | False |
| WS-18751138294f1fff-fact_brief | baseline | completed | completed_draft | 7614e9ccd1a1de68e5e78323602bb8226c00821057c15dd350b89645ecf8570f | stop | missing | 400 | False | False |
| WS-18751138294f1fff-fact_brief | candidate | completed | completed_draft | d34bfb39475582271dcb5b479fecf24601d7c589c7edf7e3d766f0c1e07b8d97 | stop | missing | 428 | False | False |
| WS-18751138294f1fff-evidence_progress | baseline | completed | completed_draft | 689cb661ccad87c9056896afe9c9e263e170b64b206ecc46d66306e0713652a0 | stop | missing | 441 | False | False |
| WS-18751138294f1fff-evidence_progress | candidate | completed | completed_draft | e922ad726378d390c41c4958a46e28bd7ccd76a6127e082d7d27fe07686582bb | stop | missing | 438 | False | False |
| WS-b1404e80bbf35ff5-fact_brief | baseline | completed | completed_draft | 8f5d64ab4cd780a739d8dc8599291f844a76842ba7fcf1e44213a8e904a31068 | stop | missing | 329 | True | False |
| WS-b1404e80bbf35ff5-fact_brief | candidate | completed | completed_draft | 1704c83a14c9c2c023e055d1987f5ccc8ed4a0f35b20fa936166ecb1832936df | stop | missing | 577 | False | False |
| WS-b1404e80bbf35ff5-evidence_progress | baseline | completed | completed_draft | c5c608cac8540957fa5bc3d9116d1701fff20d78ceaa0034199c7fa968408f35 | stop | missing | 508 | False | False |
| WS-b1404e80bbf35ff5-evidence_progress | candidate | completed | completed_draft | aadbd0f536ffee13bf10e4068f9e492f30668f6cfa85a3feef03fcd4478e7583 | stop | missing | 444 | False | False |
| WS-7063f77d23d14f7d-fact_brief | baseline | completed | completed_draft | ab1c76bd26208d43990d056aaa25459b56d2a32b38b3e0ebe044fb380858e9ef | stop | missing | 635 | False | False |
| WS-7063f77d23d14f7d-fact_brief | candidate | completed | completed_draft | e0f031606d32a9c23d4db8af26c95121d4d219632ea966f4679da8f4102eef3a | stop | missing | 525 | False | False |
| WS-7063f77d23d14f7d-evidence_progress | baseline | completed | completed_draft | b92d9984637728404ce197b92dd0196ffcd89e6ff845da3f889714659f1a6f7a | stop | missing | 580 | False | False |
| WS-7063f77d23d14f7d-evidence_progress | candidate | completed | completed_draft | f63c9ebf09faefa01f82438da589f6a7c12e357cf46ef3883a3e96d7fdc09181 | stop | missing | 587 | False | False |
| WS-0b45e2e5e4e27a20-fact_brief | baseline | completed | completed_draft | 57d8375a08a5b17e14ddfc3727093de291e71ff92353eb70b74c1748d3db2674 | stop | missing | 415 | False | False |
| WS-0b45e2e5e4e27a20-fact_brief | candidate | completed | completed_draft | c51cf6cbd8e16a475e0c172cddda02947e8ae7a83284749cf17567e7927cbcba | stop | missing | 440 | False | False |
| WS-0b45e2e5e4e27a20-evidence_progress | baseline | completed | completed_draft | 69145bc4f73c2e8cf7982e7fd7f65f927867a1cdb8a7fdf59d3fb068ec99ff52 | stop | missing | 466 | False | False |
| WS-0b45e2e5e4e27a20-evidence_progress | candidate | completed | completed_draft | c284a6280e2147cf3c6f0584488d312965de18be50fbe5a6ccfd1e26221fcaf6 | stop | missing | 490 | False | False |
| WS-ef74d81b410a10f9-fact_brief | baseline | completed | completed_draft | 10f8e923f2cfecb22629b200319c2429b3027e96f1e0c129e43a9cc62c585214 | stop | missing | 450 | False | False |
| WS-ef74d81b410a10f9-fact_brief | candidate | completed | completed_draft | 30a0952ed5b681b4f1d471021cbaac1a182dd0ef79e46c5e9f79657f85a63793 | stop | missing | 363 | False | False |
| WS-ef74d81b410a10f9-evidence_progress | baseline | completed | completed_draft | ecafb66c05a3da29b9c8b4b7ddb4d0009dde3176250c656b57cc844ad191c8ca | stop | missing | 454 | False | False |
| WS-ef74d81b410a10f9-evidence_progress | candidate | completed | completed_draft | 2c08ea3de3a8e5f22e71133ec3251261c5020b592cdd20d9228503524d7d947f | stop | missing | 464 | False | False |
| WS-05813dcc6da97052-fact_brief | baseline | completed | completed_draft | 507358bf623bf76b64bbbe54a78825477c358ff2fe48b00725997c000eba7b13 | stop | missing | 946 | False | False |
| WS-05813dcc6da97052-fact_brief | candidate | completed | completed_draft | 7e2c8b303a3079989ab1e78783796257a1ea8bb1f3639228aeeeeddc9782f925 | stop | missing | 913 | False | False |
| WS-05813dcc6da97052-evidence_progress | baseline | completed | completed_draft | 0f793b3692781b90341ee3b4c60cedf37a731873a6dee4c94e43cc57e2d75318 | stop | missing | 493 | False | False |
| WS-05813dcc6da97052-evidence_progress | candidate | completed | completed_draft | 48a7fc37b522330517b100a8b156c2c812a8a78ad80e12e993ee5faa2f763eaf | stop | missing | 949 | False | False |
| WS-847ce31d7838c1d5-fact_brief | baseline | completed | completed_draft | 3c9f0edeaada02fe2fa9354e53e2a80e1f0f1eaafd1477c8643fe5b2cbdffc11 | stop | missing | 392 | False | False |
| WS-847ce31d7838c1d5-fact_brief | candidate | completed | completed_draft | e9a94a125193b66fb75028d3b1dcbad881e1cc991715b39e6424daf6c8517022 | stop | missing | 405 | False | False |
| WS-847ce31d7838c1d5-evidence_progress | baseline | completed | completed_draft | 568f7cdd7484e1a639f08317680e86ee28658cefa7e41deb5512aa89d2c81b87 | stop | missing | 558 | False | False |
| WS-847ce31d7838c1d5-evidence_progress | candidate | completed | completed_draft | 4126a3938eb903a293169d0777e5ef9fd67c12a2468bb29df69f0c821dee4d15 | stop | missing | 505 | False | False |
| WS-a7480950777d6f1e-fact_brief | baseline | completed | completed_draft | 7d0451e2423506001d096e0055692c8baca6f93a4c0677c66d19d362dc66e460 | stop | missing | 507 | False | False |
| WS-a7480950777d6f1e-fact_brief | candidate | completed | completed_draft | ef264cc95d223658bc67d11df6175f9ac1b1e7376e64b1e24b19dbadd1db7e8e | stop | missing | 451 | False | False |
| WS-a7480950777d6f1e-evidence_progress | baseline | completed | completed_draft | 48cdcd84f8615ca8ed6325162d93a5f2b4b1658b4ce6e547e7dca2d9488e32e3 | stop | missing | 483 | False | False |
| WS-a7480950777d6f1e-evidence_progress | candidate | completed | completed_draft | 1ca43c2dfa9260ccfcf2767e03cad847a7bbe7c19106b200aab97d1b7546fd4c | stop | missing | 500 | False | False |
| WS-5fd1fe6972d5c019-fact_brief | baseline | completed | completed_draft | 42ba4edb2cef010a23073db673d8a5b1bd9e9ca72f4dcfff4947b2cafef39220 | stop | missing | 431 | False | False |
| WS-5fd1fe6972d5c019-fact_brief | candidate | completed | completed_draft | b9c53e0e3c894412cc72b4f8e35f909be0b69539febe3e0d8ee96792519d3154 | stop | missing | 426 | False | False |
| WS-5fd1fe6972d5c019-evidence_progress | baseline | completed | completed_draft | 8c88069ef2ba8f0da52a73d8eb044d209990ebf8b570b33c6d2633fbafe44a38 | stop | valid | 490 | False | False |
| WS-5fd1fe6972d5c019-evidence_progress | candidate | completed | completed_draft | b6b2e4909aa02473f2ea5fba3bdc85a373c35faf22314b58ac8c12936fc7ee23 | stop | missing | 517 | False | False |
| WS-93a29cc654aaea6d-fact_brief | baseline | completed | completed_draft | 5566143ade557d20fbce9ad765ca1530779873a7318884628dddab7e7bc5929b | stop | missing | 590 | False | False |
| WS-93a29cc654aaea6d-fact_brief | candidate | completed | completed_draft | 298c99d19a15f6c3942ffe2527b828a9a2129993815bc73279f5847b3378b0c2 | stop | missing | 695 | False | False |
| WS-93a29cc654aaea6d-evidence_progress | baseline | completed | completed_draft | e7cde79fa2fcf758f46e3ec59aa04b946aa6f8112970484640bdcf42aec4eaaf | stop | missing | 675 | False | False |
| WS-93a29cc654aaea6d-evidence_progress | candidate | completed | completed_draft | 9f82d218f5df7b4b16ee9ccfc6395b5a23165ec81fddf87baaaa42714cc9ee88 | stop | missing | 575 | False | False |
| WS-2d856e2439bd7bd2-fact_brief | baseline | completed | completed_draft | 0c97d9861f06866a2b238ade264455b34043c0950188cebc2d7375bc6ca384c1 | stop | missing | 350 | True | False |
| WS-2d856e2439bd7bd2-fact_brief | candidate | completed | completed_draft | ba3fd209cb639114120d30823e8fd75d9d1845db26c478fcd980adebc6b2c06c | stop | missing | 392 | False | False |
| WS-2d856e2439bd7bd2-evidence_progress | baseline | completed | completed_draft | 22a7799c2b74b7aa7db71545b5a0fe2b899161b1d29d7b6842de967827d47094 | stop | missing | 381 | False | False |
| WS-2d856e2439bd7bd2-evidence_progress | candidate | completed | completed_draft | 27b527f0024dff09683808e91a7a82cf04780bb38a7be4d1ce60142e5e5e576f | stop | missing | 514 | False | False |
| WS-eda34e7aefef4380-fact_brief | baseline | completed | completed_draft | f9fba7d699812d6cfa8345eef44d3631fc835b33dadb7e9c28b10a7878deae0c | stop | missing | 447 | False | False |
| WS-eda34e7aefef4380-fact_brief | candidate | completed | completed_draft | 20672cabaa187dc3ea06a70fa78897214ffdb2c202415beb13c149539b42b17a | stop | missing | 480 | False | False |
| WS-eda34e7aefef4380-evidence_progress | baseline | completed | completed_draft | b22a40b58841ef14cccd3d80a59d854cc4e1e99ba5ccdbc75102838836ddf5ab | stop | missing | 519 | False | False |
| WS-eda34e7aefef4380-evidence_progress | candidate | completed | completed_draft | 8ec3e9f8122aa69ca21cfa4b6822dea5cc740fc71f6a23832ea2add928be4726 | stop | missing | 422 | False | False |
| WS-2fb2a0776c3c4768-fact_brief | baseline | completed | completed_draft | b3821e86a932b413ba5596d2b800b93e1537567a414589789c08f75e80cafc1f | stop | missing | 388 | False | False |
| WS-2fb2a0776c3c4768-fact_brief | candidate | completed | completed_draft | 56164dc0b1783acf6e0261055b21a2b70b842bda64b5f35a259b4a8855604c4f | stop | missing | 357 | False | False |
| WS-2fb2a0776c3c4768-evidence_progress | baseline | completed | completed_draft | 0bab59031f85691293e131b5c6191ce2aeea783a2e5b4774693fce5cc4391898 | stop | missing | 390 | False | False |
| WS-2fb2a0776c3c4768-evidence_progress | candidate | completed | completed_draft | 3f7e737d2d428ab1be8f688d086d2fb4c5ea3a700811e88f51c1b441e9fa10c1 | stop | missing | 459 | False | False |
| WS-15fadc6cd74a248d-fact_brief | baseline | completed | completed_draft | 0572b3f2d4acccf0effd821cb1fab4e23c7956e4a9f23e7fbbf7b576446d0cd3 | stop | missing | 367 | False | False |
| WS-15fadc6cd74a248d-fact_brief | candidate | completed | completed_draft | f3d3ef0019b1b17845b5abc01bd82b530cee451b289b7a7e669e51e489496077 | stop | missing | 311 | True | False |
| WS-15fadc6cd74a248d-evidence_progress | baseline | completed | completed_draft | e59196f961e08af5cc85bc9531d21bf139135df232d8abb12d6cc5c9ee49143f | stop | missing | 437 | False | False |
| WS-15fadc6cd74a248d-evidence_progress | candidate | completed | completed_draft | 29cc1d1773ba8ed6799a59a1325f217761471d075da7ccdb5d6074e545b2793e | stop | missing | 322 | True | False |
| WS-9ece5ed558ad066c-fact_brief | baseline | completed | completed_draft | cf6b8301aaf226ea8d93615c4e499852cb00dff06677d9ad5b7dac8a4140eef3 | stop | missing | 621 | False | False |
| WS-9ece5ed558ad066c-fact_brief | candidate | completed | completed_draft | 9954a0fc25c21fa970acd972cb82077af784c4d3e7c7c7984f55095d884c739d | stop | missing | 649 | False | False |
| WS-9ece5ed558ad066c-evidence_progress | baseline | completed | completed_draft | dda8d062cf7f3c30613b698be9e0cea71d2d11e9b64edb4b2f1248e2e56bc70e | stop | missing | 660 | False | False |
| WS-9ece5ed558ad066c-evidence_progress | candidate | completed | completed_draft | 0f310b3e6c38093beb3e7167d292e1e2ef522f92bc84f680b927ebe83830c952 | stop | missing | 757 | False | False |
| WS-ff40c05c8ee2c747-fact_brief | baseline | completed | completed_draft | be4289fe549895113597e25130402f7d5c3378b5f637b6e6b22e915149e2551e | stop | missing | 468 | False | False |
| WS-ff40c05c8ee2c747-fact_brief | candidate | completed | completed_draft | 5ccea8d1d7941945bb5d6b2a1837b7bbe56ceb246feb580d0198f7bab9eb5d6a | stop | missing | 429 | False | False |
| WS-ff40c05c8ee2c747-evidence_progress | baseline | completed | completed_draft | 9fff41589d6bfff3ba3c405199f46538a0815b244a7eff63521ac5b7a50e88ec | stop | missing | 380 | False | False |
| WS-ff40c05c8ee2c747-evidence_progress | candidate | completed | completed_draft | 625295c8eb954a43b792f674862bfd10adbc0c5ed382db0cc4174867d15af617 | stop | missing | 538 | False | False |
| WS-ffee6d2d7747d1ac-fact_brief | baseline | completed | completed_draft | ae15c17929cfbdb2969eb00caa325c48bc27a7d680b2f3e8807aba6b3b77458f | stop | missing | 602 | False | False |
| WS-ffee6d2d7747d1ac-fact_brief | candidate | completed | completed_draft | d2efd03be1f97d28df845c4448641c856c63596ca8ebad7e493ab32b11fbdd11 | stop | missing | 415 | False | False |
| WS-ffee6d2d7747d1ac-evidence_progress | baseline | completed | completed_draft | 87f59139db46e38333a77332f138b4edaea146713c7cf3dc0b813b199ee5c8ca | stop | missing | 516 | False | False |
| WS-ffee6d2d7747d1ac-evidence_progress | candidate | completed | completed_draft | 986d0a12a0aa26d627d575112e5211d0becd4c5c0a5eaf3c63b08d8b616db7a6 | stop | missing | 916 | False | False |
| WS-87d7a7caa19d4fa0-fact_brief | baseline | completed | completed_draft | b4bd66acabfee0ca97091605c5804f69b3a1aca94f66212eb5871e12bc1443cb | stop | valid | 695 | False | False |
| WS-87d7a7caa19d4fa0-fact_brief | candidate | completed | completed_draft | fd0b4ec7907d4d6558a06076b16ca4b7bf8e22a36332ae3ec655cef1cee30955 | stop | missing | 740 | False | False |
| WS-87d7a7caa19d4fa0-evidence_progress | baseline | completed | completed_draft | 5b8cc7a5e92c726b5964ec7f61ef4bf1d7fa61b121425a1e61037b635a1e9d4d | stop | missing | 669 | False | False |
| WS-87d7a7caa19d4fa0-evidence_progress | candidate | completed | completed_draft | eb599711689e3d36e7093d1d6c7e03570fec4563dca8f6d46a5c7ed8769e4754 | stop | missing | 882 | False | False |
| WS-e8b4b095f24249cc-fact_brief | baseline | completed | completed_draft | 940b1543142a6141d8a06851dee4e9e49eda9d6661b6562f40c726c61ed8acd7 | stop | missing | 567 | False | False |
| WS-e8b4b095f24249cc-fact_brief | candidate | completed | completed_draft | d96a0fb13bdfa9c556cdfb55bd4aa4c720929ed6ba0f698bbca4f6e6db0b3a26 | stop | missing | 495 | False | False |
| WS-e8b4b095f24249cc-evidence_progress | baseline | completed | completed_draft | cd3cebf3db267937d3c1e23ce5e962892fc51406ac5450051c7b25ace2ddf51b | stop | missing | 591 | False | False |
| WS-e8b4b095f24249cc-evidence_progress | candidate | completed | completed_draft | 399770043d1eea15ea2a46550d47cc558721693cad286341254aaf31dcfa76f8 | stop | missing | 537 | False | False |
| WS-eb56825a3bf36e9d-fact_brief | baseline | completed | completed_draft | 53f4d2782f247aa8e3dfc76b9e12e54ea735491692bac8997e65e9ad8dc21b59 | stop | missing | 400 | False | False |
| WS-eb56825a3bf36e9d-fact_brief | candidate | completed | completed_draft | db72a31eaf6a78f1f29517be65522ddf5175c6eec1e1976eb0259b45fe91e293 | stop | missing | 499 | False | False |
| WS-eb56825a3bf36e9d-evidence_progress | baseline | completed | completed_draft | 63069c3bd08684022550e8e310b16a2990bfd16176b9c9e32e23852f3b4faa6c | stop | missing | 427 | False | False |
| WS-eb56825a3bf36e9d-evidence_progress | candidate | completed | completed_draft | 80ac581a6a45dc1d0042d6fc26346e1274627fcc1444f2c950e05448f14753d5 | stop | missing | 431 | False | False |
| WS-1d13f7ce5d07bca9-fact_brief | baseline | completed | completed_draft | fdb6f8dcf490c715a3ee53c9cee1d1069f4026035837ff866799a434c830e499 | stop | missing | 377 | False | False |
| WS-1d13f7ce5d07bca9-fact_brief | candidate | completed | completed_draft | cfae2542f4bca24a193718d3b8a3f0526a4b2dca868d921600ba68ec5a90f33d | stop | missing | 456 | False | False |
| WS-1d13f7ce5d07bca9-evidence_progress | baseline | completed | completed_draft | 1144263512c848fb7f7c5f45e4559881afd7f6dc968e93ca577b188bd7584875 | stop | missing | 412 | False | False |
| WS-1d13f7ce5d07bca9-evidence_progress | candidate | completed | completed_draft | bc4bf4130d42372cafa76bce87be612f102425311453b5ef573f3281e468d741 | stop | missing | 423 | False | False |
| WS-6388324ccfcb6700-fact_brief | baseline | completed | completed_draft | 50a6abfb7bcd4d58dd909f43214c19318ace4e3e51d46f9eb1f0845adc48ada2 | stop | missing | 580 | False | False |
| WS-6388324ccfcb6700-fact_brief | candidate | completed | completed_draft | feb1b737c74b2eefcb099b5b7acb24658a0754d0440aedd9b802be79b76eedb7 | stop | missing | 461 | False | False |
| WS-6388324ccfcb6700-evidence_progress | baseline | completed | completed_draft | 55aa1857a13d6d40a37662f8736f57fea83cc37fc6af56a2fcd83503379da773 | stop | missing | 573 | False | False |
| WS-6388324ccfcb6700-evidence_progress | candidate | completed | completed_draft | 9934072a83b852e3cb7b75870a78bf28753a13ae1d57e983b019396b854735c2 | stop | missing | 513 | False | False |
| WS-70029040eb742b3e-fact_brief | baseline | completed | completed_draft | 50c6ac2a7e1467976ceb69a584b2abbaeb874bda47c7a6a5604ea676a7dd2e8a | stop | missing | 461 | False | False |
| WS-70029040eb742b3e-fact_brief | candidate | completed | completed_draft | adfd4bbcf3f06bb05a91f7eb9b9fd82470ab65c6e0a7d4b544ec8dd6496e823b | stop | missing | 408 | False | False |
| WS-70029040eb742b3e-evidence_progress | baseline | completed | completed_draft | 69e62da32c22617bc8f067ab679af3f7a56f816054c178ec58426136ee3c783e | stop | missing | 474 | False | False |
| WS-70029040eb742b3e-evidence_progress | candidate | completed | completed_draft | a9f42ad6ed32a52af1b9bba390d2b40657aba5dd2583139e18c14d572c6bce12 | stop | missing | 503 | False | False |
| WS-3da3fc68d5672fe4-fact_brief | baseline | completed | completed_draft | eb20dd1724289431a387f1c5f2362d2f75b01be5338d38c02705cf9888f3f995 | stop | missing | 518 | False | False |
| WS-3da3fc68d5672fe4-fact_brief | candidate | completed | completed_draft | 066e5dd9ee2262d542f49f869ea041f5218c2bd8b97efac5f93e0ff7e3cfc916 | stop | missing | 523 | False | False |
| WS-3da3fc68d5672fe4-evidence_progress | baseline | completed | completed_draft | bda2d53bcb0ce14821bab56ee2165737f18de52ee5304dfd7bdc5db3eca492ed | stop | missing | 582 | False | False |
| WS-3da3fc68d5672fe4-evidence_progress | candidate | completed | completed_draft | 066e5dd9ee2262d542f49f869ea041f5218c2bd8b97efac5f93e0ff7e3cfc916 | stop | missing | 523 | False | False |
| WS-ee22c244eacb53f6-fact_brief | baseline | completed | completed_draft | ad423417788463c94428086957dadd967ed8a5aeb53a41dab2da14b1fb3d85e5 | stop | missing | 385 | False | False |
| WS-ee22c244eacb53f6-fact_brief | candidate | completed | completed_draft | a5a8d825d675ff1638a8d1327c24ac79dd9d369c43a4b428cadf97c3b9464a0e | stop | missing | 468 | False | False |
| WS-ee22c244eacb53f6-evidence_progress | baseline | completed | completed_draft | 2f1f40a7a88f90bf11a3e3aafd5ecbd1b0498355749c95608633faa3df24a449 | stop | missing | 531 | False | False |
| WS-ee22c244eacb53f6-evidence_progress | candidate | completed | completed_draft | 45b9d7c6ec5d70905d5f0fea09cbdd3c3b1905c49e1328cdee1a13c70367fea7 | stop | missing | 510 | False | False |
| WS-4c1b15f6526d0cbf-fact_brief | baseline | completed | completed_draft | 93b2e09e1d75ff9cc8212a9f35d41a55162e60b6550587df84f0aa1270910858 | stop | missing | 623 | False | False |
| WS-4c1b15f6526d0cbf-fact_brief | candidate | completed | completed_draft | d38d4a50d2ea4559ffe719b9bbc8095bccdba0b955347e6d3f8bb00b3ae3d6e7 | stop | missing | 423 | False | False |
| WS-4c1b15f6526d0cbf-evidence_progress | baseline | completed | completed_draft | 923c46b54c9e1687c0a969db178da4d5ae6a2df629280a62fe9a2205d788e5e8 | stop | missing | 615 | False | False |
| WS-4c1b15f6526d0cbf-evidence_progress | candidate | completed | completed_draft | c133316470c06720d121032df938f10e8f2d4712f596d827ed429fe1254be879 | stop | missing | 410 | False | False |
| WS-e51689ae02ce15ae-fact_brief | baseline | completed | completed_draft | 99ff0e61875b2caad9ef3130b4d848ca49999daef2acca77e164d5da54f9901f | stop | missing | 551 | False | False |
| WS-e51689ae02ce15ae-fact_brief | candidate | completed | completed_draft | 6747119f0a8d7790f59d7ca5be4c93975c33baf74f576783448d6f6475ca9462 | stop | missing | 470 | False | False |
| WS-e51689ae02ce15ae-evidence_progress | baseline | completed | completed_draft | 2f44fd4a022669cf3428cd4d9777f215c62fb446177ece3fb416a031dc1cdd25 | stop | missing | 588 | False | False |
| WS-e51689ae02ce15ae-evidence_progress | candidate | completed | completed_draft | 1389794b40b83b8183d1f722c0062c53061e9a0e0d39e07470af2784e6c47d5d | stop | missing | 525 | False | False |
| WS-3de8bcbd13056fe5-fact_brief | baseline | completed | completed_draft | e42d2d2125787b753b1dd34aa36263035a2af5f7b8d2ef31c8818eaa1d2f1999 | stop | missing | 443 | False | False |
| WS-3de8bcbd13056fe5-fact_brief | candidate | completed | completed_draft | 1e5a116201d5257bc6447cdaf4145bfc58269e2b689c343246ca47e01b5d593f | stop | missing | 457 | False | False |
| WS-3de8bcbd13056fe5-evidence_progress | baseline | completed | completed_draft | 0050baf1efcd78b9eae69c7ec7fc662b3647c75ce0a20c645675d303ba3ac50f | stop | missing | 384 | False | False |
| WS-3de8bcbd13056fe5-evidence_progress | candidate | completed | completed_draft | 23e87aae64c8249d4859f0ec6e0a7db5919ee5f6722803564d79235eb53c642c | stop | missing | 477 | False | False |
| WS-9054515a094f5509-fact_brief | baseline | completed | completed_draft | 68e31fd6ac3838268b38d392a18304d21063dfa4ba4d58bb4b72aa688cac50fe | stop | missing | 360 | False | False |
| WS-9054515a094f5509-fact_brief | candidate | completed | completed_draft | cf858b97cdb8f51ad906c415a5f097bf6079791deab3709af7d9557e6c889b51 | stop | missing | 348 | True | False |
| WS-9054515a094f5509-evidence_progress | baseline | completed | completed_draft | 3d5c574bf21bd08ec656a020a21fd2187d79a3b90e825c0f5bac98695eb28f9c | stop | missing | 387 | False | False |
| WS-9054515a094f5509-evidence_progress | candidate | completed | completed_draft | 667ba85b2f1b6df7d1d333b61c7d02110d0078819c0fc279daf741ecbe805437 | stop | missing | 336 | True | False |
| WS-c4cbedfcf4d9e50e-fact_brief | baseline | completed | completed_draft | 1c5a8300dc289987ce3f96c84d3f5f8a6dc53c1e95845cad2f8142553ed86439 | stop | missing | 467 | False | False |
| WS-c4cbedfcf4d9e50e-fact_brief | candidate | completed | completed_draft | 7d39292127ebd364fe185b8b00a41eaf10c22cfbc798bbb1f4dcb1ff14ea3c59 | stop | missing | 480 | False | False |
| WS-c4cbedfcf4d9e50e-evidence_progress | baseline | completed | completed_draft | 04df8d0177b4fe8bf6fe77c6a478aff2423b9d43b316d12a7e47437474ce0f12 | stop | missing | 514 | False | False |
| WS-c4cbedfcf4d9e50e-evidence_progress | candidate | completed | completed_draft | 43229de2162d0e6b010cce18a45065c85e8349bdbda387ea4a05be3d3d82e9ab | stop | missing | 356 | False | False |
| WS-508f18c5a673050d-fact_brief | baseline | completed | completed_draft | 97ff815479e23e3ff1def8a797ea00493eb86acadeceb9d61033b05f02841b34 | stop | missing | 603 | False | False |
| WS-508f18c5a673050d-fact_brief | candidate | completed | completed_draft | bff4663cac7df53dbcc5e531fd9524d49b9f87098b8cd0aef603594491f72460 | stop | missing | 707 | False | False |
| WS-508f18c5a673050d-evidence_progress | baseline | completed | completed_draft | a9236bba7646167f6dd33470d90e64b9393f39808bd77f98506221261c1485af | stop | missing | 541 | False | False |
| WS-508f18c5a673050d-evidence_progress | candidate | completed | completed_draft | a4922113e1f6b0af2ec76df2a8630fc733efad7b261852d910c51600aa64009d | stop | missing | 839 | False | False |
| WS-59a84090977c61cd-fact_brief | baseline | completed | completed_draft | 596f7f97d13820bde06691127f9d0e801301fe8e8c8effc2e55f1fbfafeda01c | stop | missing | 753 | False | True |
| WS-59a84090977c61cd-fact_brief | candidate | completed | completed_draft | 82ab8cf854af3d97b2eae092e6393ecd191bd58dafcddf1b8ab87d9d624c6168 | stop | missing | 627 | False | False |
| WS-59a84090977c61cd-evidence_progress | baseline | completed | completed_draft | 68c789df77cc7cb1424c27f4a4821b3647e3552c47fc0f0cd467727653da9e1f | stop | valid | 822 | False | True |
| WS-59a84090977c61cd-evidence_progress | candidate | completed | completed_draft | b66a76358168b03f85269f7389621b87564b617e022473c04814a2193f3b762b | stop | missing | 699 | False | False |
| WS-1cd15ffb291b4e48-fact_brief | baseline | completed | completed_draft | 42ca1c47cd4919603a6d0decfbf46304955f5061bf2cefb879371d4e29e4bd75 | stop | missing | 610 | False | False |
| WS-1cd15ffb291b4e48-fact_brief | candidate | completed | completed_draft | 0b3fd6bc92b51d436bb0a0acd8532c17b6216d51befdf3a81d9fd70a72782329 | stop | missing | 594 | False | False |
| WS-1cd15ffb291b4e48-evidence_progress | baseline | completed | completed_draft | a521b007591632eecd4c3b86a704798470579b0582c9cbf4bbbcf925f1d7ca12 | stop | missing | 684 | False | False |
| WS-1cd15ffb291b4e48-evidence_progress | candidate | completed | completed_draft | 2a4dbb3131366547c967c11c7f5075e5285cb72cb32595f4b3307e1f5b1ef1be | stop | missing | 771 | False | False |
| WS-2bc4127fceb73a3a-fact_brief | baseline | completed | completed_draft | 84fd0552365f08d438ffbaab7541ddbe5d999398675e0d80cac61ec97c3fedec | stop | missing | 326 | True | False |
| WS-2bc4127fceb73a3a-fact_brief | candidate | completed | completed_draft | 1d77c8c4dedefb00b078727502bc40a7dd8cbd684298c17c1764f88f20e6714c | stop | missing | 336 | True | False |
| WS-2bc4127fceb73a3a-evidence_progress | baseline | completed | completed_draft | 7ce52dcdc84aaf51a43f1507b593e3e5eb970ecb8e123175c21e397cbf211837 | stop | missing | 366 | False | False |
| WS-2bc4127fceb73a3a-evidence_progress | candidate | completed | completed_draft | b3915f7bcbd0deff6c478afc7e257a09c2834a9c4b45fce4cd7f533ce562258e | stop | missing | 346 | True | False |
| WS-7aa0bb5b7885783d-fact_brief | baseline | completed | completed_draft | 13ab4314eb8eccb086c7ae25fa4ca24b4bccffa982e7be5e524821a0f98302d8 | stop | missing | 454 | False | False |
| WS-7aa0bb5b7885783d-fact_brief | candidate | completed | completed_draft | 2dbf6bf2f2718e715041e6b15626668d15f4ccae509a20de6e192eb26ec0f484 | stop | missing | 375 | False | False |
| WS-7aa0bb5b7885783d-evidence_progress | baseline | completed | completed_draft | f0c8350a100ba1a9bf876194fd5d5a0c21b05943dd02cadc8ec6646eda2770e1 | stop | missing | 403 | False | False |
| WS-7aa0bb5b7885783d-evidence_progress | candidate | completed | completed_draft | 115ce46d4a086f3324f1f878b989cc0ef4f6d2778132f344084a7dfeefe69fc9 | stop | missing | 416 | False | False |
| WS-bbd7ea323cdcd7a7-fact_brief | baseline | completed | completed_draft | 53c9780f075a3bc5356216654e22b48ac717a6d641a02131a531d6de8c8e60f3 | stop | missing | 394 | False | False |
| WS-bbd7ea323cdcd7a7-fact_brief | candidate | completed | completed_draft | 509526d520990db70102ab53ddb6e178c9c07032b1e56f7059030785046f674f | stop | missing | 360 | False | False |
| WS-bbd7ea323cdcd7a7-evidence_progress | baseline | completed | completed_draft | 9997287da3bbe1c9998b0748ae9e7b90987905ed06942e6be155a58ff316961d | stop | missing | 386 | False | False |
| WS-bbd7ea323cdcd7a7-evidence_progress | candidate | completed | completed_draft | 83ce720684b099d13c762f558df97bdb9215212d50c05c17a65da3e7c92d422b | stop | missing | 358 | False | False |
| WS-4b5ba649d2d51548-fact_brief | baseline | completed | completed_draft | 13d6e19846e22e8e18bbb2f2cdc71311c5ec733b3e828468de8b5df5cd0c78bc | stop | missing | 446 | False | False |
| WS-4b5ba649d2d51548-fact_brief | candidate | completed | completed_draft | dda982fca619b50ac13695c228bf90128fe18029b3d4bf7e5f78316ec97d9c2b | stop | missing | 468 | False | False |
| WS-4b5ba649d2d51548-evidence_progress | baseline | completed | completed_draft | 56d00a7c6cae281a24cda0ea708b4f027c53cef13385fd2b040a06e8d8709621 | stop | missing | 486 | False | False |
| WS-4b5ba649d2d51548-evidence_progress | candidate | completed | completed_draft | 21f426d48a3c29ac4be3ff3f795e46487b7597d74a4185f1b6d490e0bafea3a6 | stop | missing | 511 | False | False |
| WS-d15b65306f0a8298-fact_brief | baseline | completed | completed_draft | 7fe4af5d06c39cccf2e810e88353725f8c2d4a60106b4f7bd550e6cb3797f533 | stop | missing | 600 | False | False |
| WS-d15b65306f0a8298-fact_brief | candidate | completed | completed_draft | b079452071def7ef9d0349460327204fa377687c6181c69bcedc5457380b624f | stop | missing | 671 | False | False |
| WS-d15b65306f0a8298-evidence_progress | baseline | completed | completed_draft | 9ff1f288b54f6cc35762b9ff5f2570a465e126f36cd5766b94b6369570ac80e8 | stop | missing | 595 | False | False |
| WS-d15b65306f0a8298-evidence_progress | candidate | completed | completed_draft | 84765a4f879540d2a37692c86c9bb72a5db1caa5ff6bf7bb7745f75ab0c7c19f | stop | missing | 574 | False | False |
| WS-2dfa4e38ab6b75da-fact_brief | baseline | completed | completed_draft | ab9cd955611f095929f679874e9fe5d02a2cc644116d1c0e3c71aaaf952680c0 | stop | missing | 322 | True | False |
| WS-2dfa4e38ab6b75da-fact_brief | candidate | completed | completed_draft | c6a023b8d1640f1dd63d16c27af09c1ddc8b081fe4ce1b4ce34ecda23666267d | stop | missing | 382 | False | False |
| WS-2dfa4e38ab6b75da-evidence_progress | baseline | completed | completed_draft | 6832eb019a12336b556430268044f623dacaa5913a85501f71c9fed1555f16ab | stop | missing | 381 | False | False |
| WS-2dfa4e38ab6b75da-evidence_progress | candidate | completed | completed_draft | 24809c854a1419fa3eaeafde5c61b0f401aea28e37cebe7950df369530624e20 | stop | missing | 381 | False | False |
| WS-3d0a55c4c4b4915b-fact_brief | baseline | completed | completed_draft | 6538218c7a3d3c85a5da1ffae71021e7e0789dcd25380fbbab87da19befacb5c | stop | missing | 518 | False | False |
| WS-3d0a55c4c4b4915b-fact_brief | candidate | completed | completed_draft | 0ae500b251fdbc1ca4ddd89c7ae89766a67413e58635ffa289b6e364f94e6094 | stop | missing | 461 | False | False |
| WS-3d0a55c4c4b4915b-evidence_progress | baseline | completed | completed_draft | f2cf8cfa0b869b7959f52b25760fd4c57ff77c06b932ed27579622c69349a81d | stop | missing | 627 | False | False |
| WS-3d0a55c4c4b4915b-evidence_progress | candidate | completed | completed_draft | 92957a306334bcb3438b8bc95a69f73594e692627a5f0edd361e74614a22f2a0 | stop | missing | 384 | False | False |
| WS-963ba890f58e9373-fact_brief | baseline | completed | completed_draft | 5d13b861ecf6f4878ab2459d83f870081e8f58055bdb4ca1197b6f528684d8f3 | stop | missing | 379 | False | False |
| WS-963ba890f58e9373-fact_brief | candidate | completed | completed_draft | f960bbf0dee15e33bde8eb5ff0aa3df65efbad3821e40cc89f38191a5d2bf202 | stop | missing | 314 | True | False |
| WS-963ba890f58e9373-evidence_progress | baseline | completed | completed_draft | 2d2d428d19171a5412f425a4f6b144fbbfb507d23ef1fac2c0f143c37052fb9f | stop | missing | 578 | False | False |
| WS-963ba890f58e9373-evidence_progress | candidate | completed | completed_draft | c806a1660330dba08b850c948ff6771f7623a20775196ea034acc054a5e0b4c5 | stop | missing | 399 | False | False |
| WS-6f589738fc2f1b80-fact_brief | baseline | completed | completed_draft | 79f72579461edc850be0fc62fb0839ba00bc2c84a427922d47bbc9b762291c58 | stop | missing | 468 | False | False |
| WS-6f589738fc2f1b80-fact_brief | candidate | completed | completed_draft | 5fd844059014cb59d2e46319fb0914e69e074cb5954de9c2349ed82a211db02d | stop | missing | 510 | False | False |
| WS-6f589738fc2f1b80-evidence_progress | baseline | completed | completed_draft | 68606625c9b46b769ad18496b3f1b0a7f4a299b1b223a734d5db88e90e53ed21 | stop | missing | 608 | False | False |
| WS-6f589738fc2f1b80-evidence_progress | candidate | completed | completed_draft | 47c454b5fa8257a5dca664583497faca667688cb95880817a2df5bca87e3a144 | stop | missing | 508 | False | False |

Algorithm SHA256: 75bdbf1ca6e7a004677e6c698a144e2dc5e21b9568d2eda30c2e6d0196942048

Tool SHA256: 48db897e82b903c8b50a4d6aaccf45e1542891bda07517fb63a20e2e641efcbc

Frozen plan SHA256: 7d41fe8c9cfedb2794cba0405b051ff41ceee3083718c2156b1ea2aef8d51e7f
