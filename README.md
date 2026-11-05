# clickup-gh-workflow

A standalone CLI for ClickUp cards, subtasks, schedules, estimates, tracked time and GitHub PR delivery. Works with people and any AI agent that can run shell commands; no provider SDK or browser is required.

เครื่องมือกลางสำหรับเชื่อมงาน ClickUp กับ GitHub ใช้ได้จาก Terminal หรือ AI ที่รันคำสั่งบนเครื่องเดียวกันได้ โดยแต่ละโปรเจกต์มี config ของตัวเอง

## Requirements

- Python **3.11+**
- Git
- GitHub CLI (`gh`) และบัญชีที่เข้าถึง repository/fork ของคุณได้
- ClickUp API token ของบัญชีคุณเอง
- GitHub mode: เปิด Actions และตั้ง secret ใน fork ที่ระบุใน config

ตัวติดตั้งแบบ shell เน้น macOS/Linux; Windows ยังไม่ได้ทดสอบโดยตรง Runtime ใช้ Python standard library ไม่มี runtime dependency เพิ่ม ไม่มี Android SDK, Codex SDK หรือ Team Plugin ที่ต้องติดตั้งก่อน

## 1. Install

### Source installer — ไม่ต้องลง Python package เพิ่ม

```sh
gh auth login
gh repo clone dang-thairun/clickup-gh-workflow
cd clickup-gh-workflow

# Preview: แสดงตำแหน่งที่จะติดตั้ง
python3.11 scripts/install.py

# ติดตั้งจริง
python3.11 scripts/install.py --apply

# ให้ Terminal นี้ค้นพบ executable
export PATH="$HOME/.local/bin:$PATH"
clickup-gh-workflow --version
clickup-gh-workflow --help
```

Repo อาจเป็น Private: ต้องได้รับสิทธิ์ก่อน clone

ตัวติดตั้งสร้าง:

- `~/.local/bin/clickup-gh-workflow`
- `~/.local/share/clickup-gh-workflow/` — runtime และ README
- `~/.agents/skills/clickup-gh-workflow/SKILL.md` — คำแนะนำกลางสำหรับ AI

ไม่แก้ shell startup หรือ config ของ AI provider ให้เอง การตั้ง PATH แบบถาวรให้เพิ่มตาม shell ที่ใช้ หากพบคำสั่งหรือ Skill เดิมที่ตัวติดตั้งนี้ไม่ได้เป็นเจ้าของ โปรแกรมจะหยุดแทนการเขียนทับ ใช้ `--prefix` และ `--skill-dir` เพื่อเลือกตำแหน่งแยกได้

```sh
python3.11 scripts/install.py \
  --prefix "$HOME/.local/clickup-gh-workflow-standalone" \
  --skill-dir "$HOME/.local/clickup-gh-workflow-standalone/skills/clickup-gh-workflow" \
  --apply
```

### Python package — ทางเลือก

รันจาก repo ที่ clone มา:

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/clickup-gh-workflow --help
```

นี่คือการติดตั้งจาก source ใน repo นี้ ไม่ได้อ้างว่ามี package เผยแพร่บน PyPI แล้ว การติดตั้ง package อย่างเดียวไม่คัดลอก Skill ไปยังโฟลเดอร์ของ AI; ใช้ `clickup-gh-workflow skill` เพื่ออ่านคำแนะนำได้

## 2. Initialize the project you want to track

ไปที่โปรเจกต์แอป/บริการที่ต้องการเชื่อม ClickUp แล้วรัน:

```sh
cd /path/to/your-app
clickup-gh-workflow init
clickup-gh-workflow init --apply
```

`init` เป็น offline preview; `--apply` สร้างไฟล์ต่อไปนี้เมื่อยังไม่มี โดยไม่เขียนทับ config/แผนเดิม:

- `.clickup-workflow.toml`
- `Implementation/example-spec.md`

**แก้ค่าตัวอย่างให้เป็นของทีมคุณก่อนใช้ remote commands** ดู [config ตัวอย่าง](examples/config.toml) และ [รูปแบบสเปก](examples/example-spec.md)

```toml
workspace_id = "YOUR_WORKSPACE_ID"
list_id = "YOUR_LIST_ID"
repository = "your-org/your-app"
fork = "your-user/your-app"
base_branch = "develop"
custom_id_prefix = "TASK"

[statuses]
backlog = "backlog"
ready = "ready"
in_progress = "in progress"
in_review = "in review"
rework = "rework"

[defaults]
priority = "normal"

[delivery]
pr_unit = "spec"
required_checks = ["test", "lint", "build"]
allow_no_ci = false
```

Workspace/List ID, repository, fork และ branch ต้องเป็นค่าจริงของโปรเจกต์นั้น ชื่อ status ต้องตรงกับ ClickUp จริง `doctor` จะรายงานชื่อที่ไม่พบและไม่สร้าง status ให้เอง โดยเฉพาะ `rework` บางทีมอาจไม่มี ต้องปรับ mapping ให้ตรงขั้นตอนของทีมก่อนใช้งาน

รหัสมี **3 ชนิด**:

- `APP-010` — รหัสสเปกที่คุณกำหนดใน Markdown
- `TASK-42` — Custom Task ID ที่ ClickUp ออกให้ (`custom_id_prefix = "TASK"`)
- `abc123` — internal API ID

เลือก prefix ของสเปกและ Custom Task ID ให้ต่างกัน ค่า compatibility default ของ custom prefix คือ `DEV` หาก config ไม่ระบุ ห้ามเดาเลข Custom ID เอง; ใช้ `read` อ่านกลับหากตอน create ยังไม่ได้เลข

เพิ่ม runtime files ลง `.gitignore` ของโปรเจกต์:

```gitignore
.team-workflow/
.env
.env.*
```

Config ไม่มี token จึง commit ได้เมื่อทีมต้องการ ส่วน bindings/PR registry อาจมี task IDs และชื่อ repo จริง ให้เลือกจัดเก็บตามสิทธิ์เข้าถึงของโปรเจกต์ Runtime receipts และผลอ่าน task อาจมีข้อมูลภายใน จึงไม่ควรส่งรวมเป็นตัวอย่างสาธารณะ

จากโฟลเดอร์อื่นให้ใส่ `--root` **ก่อนคำสั่ง**:

```sh
clickup-gh-workflow --root /path/to/your-app plan
```

## 3. Authentication

### GitHub Actions mode — ค่าเริ่มต้น

เก็บ ClickUp token ใน **fork ที่ระบุใน config**:

```sh
# gh จะถามค่า secret ผ่าน stdin; อย่าใส่ token ลง command history
gh secret set CLICKUP_API_TOKEN --repo your-user/your-app

clickup-gh-workflow doctor
clickup-gh-workflow members
```

CLI ใช้ `gh` ที่ล็อกอินอยู่เพื่อสร้าง branch/commit และรัน scoped worker บน Actions ส่วน token ไม่ถูกดึงลงเครื่อง ต้องมีสิทธิ์ push branch/workflow, อ่าน Actions artifacts และสร้าง PR ใน repo เป้าหมายตามคำสั่งที่ใช้

**แม้ `read`, `doctor` หรือ preview ใน GitHub mode ก็สร้าง automation branch และ Actions run** แต่ไม่แก้ ClickUp จนกว่าจะสั่ง write พร้อม `--apply` ส่งเฉพาะ config/specs/bindings และ worker ที่ bundled มากับเครื่องมือ ไม่ส่ง `.env` หรือ source ทั้งโปรเจกต์

ผลคำสั่งมี receipt และ Actions URL หากรันค้างให้ใช้ `resume` กับ receipt เดิม แทนการยิง write ใหม่:

```sh
clickup-gh-workflow resume .team-workflow/receipts/REQUEST_ID.json
```

### Local mode — ทางเลือก

ตั้ง `CLICKUP_API_TOKEN` ใน process environment ผ่าน secret manager หรือกลไกของเครื่อง แล้วเรียก:

```sh
clickup-gh-workflow --via local doctor
clickup-gh-workflow --via local read TASK-42
```

อย่าใส่ token ในแชต, command arguments, สเปก หรือ Git ตัว CLI ไม่ใช่ OAuth/MCP server

## 4. Spec format

ไฟล์ที่อ่านคือ `Implementation/*-spec.md` โดยแต่ละการ์ดขึ้นต้นด้วย `## [SPEC-ID] English title` ดู [ไฟล์ตัวอย่างครบ](examples/example-spec.md)

- `Type / Points / Estimate`: Type หนึ่งคำ, points แบบ Fibonacci 1, 2, 3, 5 หรือ 8, estimate จำนวนเต็มหน่วยชั่วโมง เช่น `Feature / 2 / 4h`
- `Owned Paths`: paths ที่อนุญาตให้แก้ เขียนด้วย backticks
- `Dependency`: อ้างรหัสสเปกของงานที่ต้องเสร็จก่อน ห้ามอ้างตัวเองหรือสร้างวงจร
- `Subtask Estimates (minutes)`: จำนวนบวก เรียงตรง subtasks และรวมเท่ากับชั่วโมงใน header
- `Subtasks`: รายการ `  - [ ] ...` หรือ `  - [x] ...`

ชื่อการ์ดและเนื้อหาสเปกใช้ภาษาใดก็ได้ ตัวอย่างนี้ใช้หัวข้อภาษาอังกฤษ ขั้นตอน/AC เป็นข้อความในรายละเอียด ช่อง `Epic` หรือ `Type` ในรายละเอียดไม่ได้หมายความว่าตั้ง native custom field แล้ว

## 5. Cards, priorities, dates and assignees

```sh
# Offline: ตรวจสเปกและ dependency graph
clickup-gh-workflow plan

# Preview ก่อน แล้วจึงสร้างจริงทีละสเปก
clickup-gh-workflow create APP-010
clickup-gh-workflow create APP-010 --apply
clickup-gh-workflow read APP-010
clickup-gh-workflow read TASK-42

# ค่าเริ่มต้นใหม่เป็น Normal; override เฉพาะครั้งได้
clickup-gh-workflow create APP-020 --priority high --apply

# Preview edits; เพิ่ม --apply เมื่อจะบันทึกจริง
clickup-gh-workflow update TASK-42 --priority normal
clickup-gh-workflow update TASK-42 --planned-start "2027-01-15T09:00:00+07:00" --due "2027-01-16T17:00:00+07:00"
clickup-gh-workflow update TASK-42 --clear-due
clickup-gh-workflow update TASK-42 --name "Improve the example feature"
clickup-gh-workflow update TASK-42 --description-file /path/to/description.md
clickup-gh-workflow update TASK-42 --points 3

# อ่าน member ID จริงก่อนใส่ assignee
clickup-gh-workflow members
clickup-gh-workflow update TASK-42 --assignee-add 123456 --apply
clickup-gh-workflow update TASK-42 --assignee-remove 123456

# ฟิลด์ตรวจ schema จริงและแปลง dropdown label → option ID
clickup-gh-workflow update TASK-42 --field "Type=Feature"

# อ่านสถานะที่มีอยู่จริง ก่อนเปลี่ยน
clickup-gh-workflow status TASK-42
clickup-gh-workflow status TASK-42 --to "in progress" --apply
```

สร้างซ้ำจะตรวจ identity และ bind งานเดิม รวม closed/archived เพื่อกันซ้ำ ไม่รีเซ็ต priority/status ของการ์ดเดิม และไม่ลบ subtasks เกินจากแผนอัตโนมัติ

## 6. Estimates and actual time

```sh
# กระจาย estimate ตามนาทีในสเปก; parent own estimate = 0
clickup-gh-workflow estimate APP-010
clickup-gh-workflow estimate APP-010 --apply

# แก้ estimate ของ subtask ระบุ ID จริง
clickup-gh-workflow estimate TASK-43 --set 1h30m --apply
clickup-gh-workflow estimate TASK-43 --clear

# Actual time ต้องเป็นเวลาที่ทำจริง
clickup-gh-workflow time list TASK-43
clickup-gh-workflow time log TASK-43 --duration 30m --note "Reviewed the implementation"
clickup-gh-workflow time start TASK-43 --apply
clickup-gh-workflow time running TASK-43
clickup-gh-workflow time stop TASK-43 --apply
clickup-gh-workflow time cancel TASK-43 --apply

clickup-gh-workflow time edit TASK-43 --entry ENTRY_ID --duration 45m
clickup-gh-workflow time delete TASK-43 --entry ENTRY_ID --reason "Duplicate entry"
```

เมื่อมี subtasks ให้ estimate ที่ชั้นล่างสุด Parent รวมชั่วโมงได้เมื่อ ClickUp เปิด **Time Estimates Rollup** ส่วน points อยู่ที่งานที่ส่งมอบ ไม่ใช่ทุก step อย่าใส่ยอดรวมซ้ำที่ parent

`time start/stop` เป็น stopwatch ที่เก็บบนเครื่อง แล้ว post completed entry เมื่อ stop ไม่ใช่ running timer บนหน้า ClickUp เวลาเริ่ม/จบใช้เวลาผู้ใช้จริง ไม่รวมเวลาเข้า queue ของ Actions การ log/edit/delete เป็น preview จนเพิ่ม `--apply` การลบต้องระบุ entry และเหตุผล

## 7. Diff and sync

```sh
clickup-gh-workflow diff APP-010
clickup-gh-workflow sync APP-010
clickup-gh-workflow sync APP-010 --apply

# การ์ดเก่าที่ไม่มี managed markers: เก็บข้อความเดิมไว้แล้วเพิ่ม managed region
clickup-gh-workflow sync APP-010 --adopt
clickup-gh-workflow sync APP-010 --adopt --apply
```

Sync ดูแลเฉพาะชื่อและ managed spec region เก็บ manual notes, status, priority, assignee, กำหนดการ และ actual time เดิม Apply ต้องตรง remote hash ของ preview ล่าสุด หากคนอื่นแก้ระหว่างนั้นจะหยุดให้ตรวจใหม่ Estimate runtime แยกตรวจจากสเปก ใช้ `estimate` เพื่อปรับจริง

## 8. Branch, RED/GREEN and Draft PR — spec mode

โหมดเริ่มต้น `pr_unit = "spec"`: หนึ่งงานที่มีรหัสสเปกใช้หนึ่ง branch/PR และ step subtasks อยู่ใน PR เดียวกัน

ก่อน `start --apply`: Lead อนุมัติเป็น READY, dependency PRs merge แล้ว, working tree สะอาด และ config/สเปกที่อนุมัติมีอยู่บน base branch แล้ว CLI ไม่ stash/reset งานค้างให้

```sh
clickup-gh-workflow start TASK-42
clickup-gh-workflow start TASK-42 --apply

# เขียน test ที่ fail เพราะเงื่อนไขธุรกิจจริง แล้วรัน executable จริงหลัง --
clickup-gh-workflow check TASK-42 --phase red --reason "Expected behavior is not implemented" -- ./run-tests.sh

# Commit test/evidence พร้อม trailer จริง ก่อนเปิด Draft PR
# บรรทัดสุดท้ายของ commit: ClickUp-Task: TASK-42
clickup-gh-workflow pr open TASK-42 --draft --apply

# หยุดให้คนตรวจ RED ก่อน คำสั่งนี้ใช้เมื่อได้รับอนุมัติจริงแล้วเท่านั้น
clickup-gh-workflow approve-red TASK-42 --reason "Human verified the intended assertion" --apply

# พัฒนาต่อและเก็บผลปัจจุบันทุก required label
clickup-gh-workflow check TASK-42 --phase green --label test -- ./run-tests.sh
clickup-gh-workflow check TASK-42 --phase green --label lint -- ./run-lint.sh
clickup-gh-workflow check TASK-42 --phase green --label build -- ./run-build.sh

# Commit/push การเปลี่ยนแปลงพร้อม trailer ก่อนเปลี่ยนสถานะ PR
clickup-gh-workflow pr checks TASK-42
clickup-gh-workflow pr ready TASK-42 --apply
clickup-gh-workflow pr read TASK-42
clickup-gh-workflow pr sync TASK-42 --apply
```

สคริปต์ `run-*.sh` เป็นตัวอย่าง ต้องแทนด้วย test/lint/build commands ของโปรเจกต์คุณ การทดสอบที่ compile ไม่ผ่านเพราะคลาสหายไม่ใช่หลักฐาน RED ที่ดี งาน migration ต้องบันทึกประวัติจริง ไม่สร้าง RED หรือ approval ย้อนหลัง

Ready ตรวจ evidence ว่ายังตรง source, commit trailers, clean/pushed head และ CI ที่จำเป็น ไม่มี CI จะหยุดเมื่อ `allow_no_ci = false` CLI ไม่มีคำสั่ง merge

PR body เริ่ม `ClickUp-Task: TASK-42` Native PR Link/Start Date/End Date ใช้ schema/mapping จริงและ timestamp จาก GitHub แยกจาก planned start/due หากฟิลด์ไม่มีจะรายงาน pending ไม่สร้างฟิลด์เอง Merge ไม่เท่ากับ QA deployment สำเร็จหรือ DONE

## 9. Group main cards and bulk assignment

ถ้าต้องการหมวดหลัก → งานย่อย → step subtasks ให้เพิ่ม config ตัวอย่าง:

```toml
[groups.features]
name = "Example feature group"
cards = ["APP-010", "APP-020"]
```

สร้าง/ผูกงานตามสเปกให้ครบก่อน แล้ว:

```sh
clickup-gh-workflow members
clickup-gh-workflow assign-backlog --assignee 123456
clickup-gh-workflow assign-backlog --assignee 123456 --apply

clickup-gh-workflow group-backlog --assignee 123456
clickup-gh-workflow group-backlog --assignee 123456 --apply
```

คำสั่งตรวจ scope และสมาชิกจริงก่อนเขียน `group-backlog` สร้าง managed main cards แล้วจัดงานเดิมเข้าหมวด คง IDs, step relationships, assignees, status, priority และ estimates พร้อมอ่านกลับยืนยัน Assignment batch เพิ่มสมาชิกโดยเก็บ assignees คนอื่น และจำกัดเฉพาะงานที่ผูกไว้กับสเปกและ direct steps (ไม่ได้ครอบคลุม summary cards ที่ไม่อยู่ใน spec bindings)

`delivery.pr_unit = "group"` ใช้เมื่อต้องการหนึ่ง branch/PR ต่อการ์ดหมวด ตอนนี้ **automated `start` ยังไม่รองรับ group delivery** และจะหยุดแทนการเปิด child branch ผิด ใช้ group PR ที่สร้าง/ตรวจแล้วตามกระบวนการทีม และเก็บ registry เพื่อเชื่อมกลับ:

```sh
clickup-gh-workflow group-backlog --assignee 123456 --pr-file Implementation/group-prs.json --apply
```

ดู [schema ตัวอย่าง](examples/group-prs.example.json) แต่ต้องใช้ IDs/URLs ที่ตรวจยืนยันจริงของทีมคุณ ฟิลด์ `verified` เป็นข้อมูลที่ CLI ผู้เรียกต้องตรวจจริง ไม่ใช่การอนุมัติจาก Lead หรือ proof แทน GitHub คำสั่ง link ไม่สร้าง PR และไม่เปลี่ยน status/กำหนดการเอง

## 10. Use with another AI

AI ที่เข้าถึงเครื่อง/Terminal นี้ได้สามารถใช้ CLI เดียวกัน บนเครื่องอื่นต้องติดตั้งและตั้งค่า auth/config ของเครื่องนั้นเอง ไม่จำเป็นต้องลง Codex หรือ plugin เฉพาะ provider

ส่งข้อความนี้ให้ AI:

```text
Read the project's agent instructions and .clickup-workflow.toml.
Run clickup-gh-workflow skill for the provider-independent workflow guidance.
Use clickup-gh-workflow --root /absolute/path/to/project and inspect --help as needed.
Preserve user authorization and preview/apply boundaries.
Use the configured GitHub Actions mode; do not request tokens in chat.
Inspect uncertain writes and resume their receipts rather than replaying them.
```

AI แต่ละตัวอาจไม่ค้นพบ `~/.agents/skills/` อัตโนมัติ ให้ชี้ไฟล์ `SKILL.md` หรือใช้คำสั่ง `skill` โดยตรง `init` ไม่แก้ `CLAUDE.md`, `AGENTS.md` หรือ config ของ provider ให้เอง

## Known limitations

- ขั้นตอนอัตโนมัติ start/RED/GREEN/PR ใช้ spec mode; group mode เน้นจัด hierarchy และเชื่อม PR ที่มีอยู่แล้ว
- Delivery commands ต้องมี Custom Task ID; งานข้อมูลพื้นฐานรองรับ internal IDs Custom IDs ที่ยังว่างต้องอ่านกลับ/ตรวจการตั้งค่าของ workspace
- Custom ID prefix ต้องต่างจาก spec prefix เพื่อให้ resolver แยกรหัสได้ชัดเจน
- Parser ใช้รูปแบบ Markdown ที่แสดงในตัวอย่าง ไม่ใช่ตัวอ่าน Markdown ทั่วไป
- Native Type/Epic/Repository/relations และ QA-ready signal ไม่ได้ถูกตั้งให้เพียงเพราะอยู่ใน description
- GitHub mode มี latency และสร้าง automation branches/results ให้รันคำสั่งที่ List เดียวกันทีละคำสั่ง ใช้ `resume` หากรอผลค้าง
- ชุด unit tests ใช้ mocks/local disposable Git repositories ไม่ใช่การรับรองว่าทุก ClickUp plan, custom field schema หรือ OS ผ่าน live integration แล้ว
- Repository นี้ยังไม่รวม native Codex marketplace/plugin installer เดิม และไม่เปลี่ยน global CLI เดิมของผู้สร้างตอน export

## Development

```sh
python3.11 -m unittest discover -s tests -v
python3.11 -m compileall -q src
```

CI ทดสอบ Python 3.11–3.13 และตรวจการติดตั้ง package ดู [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [CHANGELOG.md](CHANGELOG.md)

## License and visibility

ยังไม่ได้เลือก/เพิ่ม license ในการแยก repo ครั้งนี้ และไม่ได้เปลี่ยน visibility ของ repo การเผยแพร่สาธารณะหรือเงื่อนไขการแจกจ่ายให้เจ้าของ repo ตัดสินใจแยกต่างหาก
