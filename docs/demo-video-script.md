# Demo video: script and shot list

**Rules this has to meet:**
- On YouTube, public or unlisted, and **under 3:00**. Aim for 2:45–2:50.
- The link must open in a signed-out browser.
- It has to show what CASCADE does, who it's for, and where AWS fits. **A feature that isn't on screen doesn't count.**

**Record it last** (Phase 6), so it can show the alerts, the CI run and the forecast replay as well.

## Before recording

- [ ] **Warm up:** run the pipeline once, 5–10 minutes before recording. The first run after a deploy takes about 80 s because the image loads cold; warm runs take about 7 s.
  ```bash
  sam remote invoke CascadePipeline --stack-name cascade-glof --region us-west-2 --event-file events/hindcast-2023.json
  ```
- [ ] **Screen:** 1920 × 1080, with browser zoom at 125–150% so text is readable. Turn off notifications and close unrelated tabs.
- [ ] **AWS console:** set the region to **us-west-2 (Oregon)**.
- [ ] **Hide in the edit:** the **account ID** (in ARNs and the account menu), the **IAM user name**, and the **email address** in the inbox shot. Blur or crop them; don't rely on hiding them live.
- [ ] **Don't film** CloudWatch's Step Functions success graph. It undercounts by one (61 against 62). Show the executions list instead.

## Shot list (about 2:50)

| Time | Screen | Voiceover |
|---|---|---|
| 0:00–0:20 | Dossier page, scrolled to the Sentinel-2 before/after images | "On the night of 3 October 2023, South Lhonak Lake in Sikkim burst through its moraine. About 50 million cubic metres of water raced 67 kilometres down the valley and destroyed the Teesta-III dam two hours later." |
| 0:20–0:30 | Top of the dossier page | "The people at that dam needed two answers: how much water is coming, and when. CASCADE gives them those answers." |
| 0:30–0:55 | The dam card | "If South Lhonak bursts, water reaches the dam 2 hours 18 minutes later, peaking at about 10,500 cubic metres a second, sixteen metres deep." |
| 0:55–1:10 | "The lake, month by month", then the map: switch between 14 Sep and 29 Oct, then Whole valley | "CASCADE measures the lake from Sentinel-2 images. When snow or ice hide part of it, it gives a range, never a false single number. The red line is the flood path it traced down the valley." |
| 1:10–1:20 | Click "Print or save as PDF", show the one-page preview | "And it prints as a one-page dossier for the control room." |
| 1:20–1:30 | `template.yaml` in the editor, scrolling the resource list | "Everything runs on AWS, deployed with AWS SAM." |
| 1:30–1:50 | Step Functions console: start an execution, then show the graph with the Map steps fanning out to green | "A Step Functions workflow fans out one Lambda job per lake-month. Lambda reads only the satellite pixels around the lake from AWS Open Data, routes the flood, and publishes the dossier." |
| 1:50–1:57 | The executions list: all succeeded | "Sixty-two runs on AWS, every one succeeded, at about seven seconds each when warm." |
| 1:57–2:07 | CloudWatch Logs REPORT lines, then the S3 `reports/` object and the DynamoDB items | "Every call is logged in CloudWatch. Results land in S3 and DynamoDB." |
| 2:07–2:15 | The SNS email arriving in the inbox | "Each new dossier is emailed through Amazon SNS. A budget alert and a failed-run alarm go to the same topic." |
| 2:15–2:22 | GitHub: the `evidence/` folder and the green CI check | "The raw AWS records behind every number are in the repo, and CI rebuilds the tables from them." |
| 2:22–2:40 | README "Results of the 2023 replay" table, then the forecast replay table from Phase 5 | "We tested it against the real 2023 flood. The lake area is within 2%, and so is the flow path. The arrival time was calibrated, and our peak runs high, so this is a screening tool, not a design." *(Add one line on the forecast replay once Phase 5 lands.)* |
| 2:40–2:50 | End card: the page URL and the repo URL | "CASCADE: glacial lake flood screening on AWS, built with Claude Code. Thanks for watching." |

## If it runs long

Cut in this order:
1. The print preview (1:10–1:20).
2. The CI shot. Keep the evidence folder.
3. The DynamoDB part of 1:57–2:07.

Never cut the Step Functions run, the dam card or the limits line.
