# BuildFuture.Me Drive Reorg Log

Timestamp: 20260516_012748
Account: niaz.gabdullin@buildfuture.me
Root ID: 1MUi2lqHSVWGtLletRTMZjRZThH_nHjQ1

Local rollback JSON: `/Users/val.vasilevsky/Git/hermes/hermes-agent/tmp_drive_reorg/buildfutureme_reorg_20260516_012748.json`
Drive rollback JSON: https://drive.google.com/file/d/1Uttnw713R5ZC4GZDdYrJp3g45iSSQAoM/view?usp=drivesdk

## Summary

- Before objects: 181
- After cleanup objects: 263
- Initial operations recorded: 178
- Duplicate-cleanup operations recorded: 24
- Created folders: 95
- File names changed: 0

## Rollback

Use the JSON log's `before_snapshot` to restore each item's old `parents` and folder `name`. Created folders are listed in `created_folders`; after moving items back, those folders can be trashed if empty. Duplicate cleanup only trashed duplicate folders; no files were deleted.

## Final tree

```text
- [D] 00_INBOX - разобрать
  - [F] Untitled presentation
- [D] 01_Project OS
  - [D] 01_Документация и планы
    - [F] Build future me документация по проекту
  - [D] 02_База знаний
    - [F] Правила операционной работы
    - [F] Принципы работы с авторами
    - [F] Принципы работы с авторами_backup
  - [D] 03_Чек-листы и SOP
    - [F] Чек-лист по запуску курсов на BUILDFUTURE.ME 
  - [D] 04_Встречи команды
    - [F] video1539151575.mp4
    - [F] video1794409590.mp4
    - [F] Созвон с Валерией 23.04.mp4
  - [D] 05_Change Logs
    - [F] buildfutureme_reorg_20260516_012748.json
    - [F] buildfutureme_reorg_20260516_012748.md
- [D] 02_Authors & Experts
  - [D] 01_Tracker
    - [F] Список экспертов для запуска на англ
  - [D] 02_Outreach & Call Scripts
    - [F] СПИЧ для авторов по партнерству
    - [F] Структура созвона со спикером
  - [D] 03_Call Recordings
    - [D] dibrain agency - женя и никита
      - [F] audio1180996768.m4a
      - [F] video1180996768.mp4
    - [D] SHOPIFY EXPERTS
      - [D] Александр Ткачев
    - [D] Азамат - digital product launch
      - [F] audio1284208846.m4a
      - [F] video1284208846.mp4
    - [D] Александр Вайб-кодинг
      - [F] audio1646882581.m4a
      - [F] video1646882581.mp4
    - [D] Дима Дрожжин
      - [F] audio1314875378.m4a
      - [F] video1314875378.mp4
    - [D] ИИ-таргетолог
      - [F] audio1466078291.m4a
      - [F] video1466078291.mp4
    - [D] Мадина (дизайн)
      - [F] audio1516189661.m4a
      - [F] video1516189661.mp4
    - [D] Олжас - ИИ-контент
      - [F] audio1645555150.m4a
      - [F] video1645555150.mp4
    - [D] Тейхан - Контент завод на ИИ
      - [F] audio1397064299.m4a
      - [F] video1397064299.mp4
    - [F] video1973376361.mp4
    - [F] АСЛАНБЕК.mp4
    - [F] Айгиз ИИ-контент.mp4
    - [F] Алишер ИИ-контент.mp4
    - [F] НАИЛЬ VFX.mp4
    - [F] Наиль Вайб Кодинг.mp4
  - [D] 04_NMS Expertise
    - [F] Запрос экспертизы от NewMindStart
- [D] 03_Legal
  - [D] 01_Templates - BFM
    - [F] BuildFutureME_Author_Agreement_EN.docx
    - [F] BuildFutureMe_Author_Agreement_RU.docx
  - [D] 02_Reference - NMS
    - [F] Author’s Agreement_template_ver 2025 (1).docx
    - [F] Fix Payment Agreement.docx
  - [D] 03_Drafts & Negotiations
    - [F] BuildFuture.Me Author Agreement - Draft
    - [F] BuildFutureME_Author_Agreement_EN.docx
    - [F] BuildFutureMe_Author_Agreement_RU.docx
  - [D] 04_Comparisons & Approvals
    - [F] BuildFuture.Me Author Agreement - Ведомость сверки
    - [F] Сверка договоров — BuildFuture.Me vs NMS
- [D] 04_Courses
  - [D] AVTP - AI Video That Pays - Ilya Makeev
    - [D] 00_Brief & Strategy
    - [D] 01_Research & Competitors
    - [D] 02_Landing Page
    - [D] 03_Promo Video
      - [D] 01_Scripts
      - [D] 02_Voiceovers
      - [D] 03_Raw Generations
      - [D] 04_Final Edits
    - [D] 04_Ad Creatives
      - [D] 01_Video Creatives
      - [D] 02_Static & Mockups
      - [D] 03_Voiceovers
      - [D] 04_Competitor Creatives
    - [D] 05_Lessons & Course Materials
      - [D] 01_Curriculum
      - [D] 02_Lesson Videos
      - [D] 03_Transcripts
      - [D] 04_Workbooks & PDFs
    - [D] 99_Archive
  - [D] DROZHZHIN - YouTube Mini-Courses
    - [D] 00_Brief & Strategy
    - [D] 01_Research & Competitors
      - [F] Транскрибация видео Али Абдал.
    - [D] 02_Landing Page
    - [D] 03_Promo Video
      - [D] 01_Scripts
      - [D] 02_Voiceovers
      - [D] 03_Raw Generations
      - [D] 04_Final Edits
    - [D] 04_Ad Creatives
      - [D] 01_Video Creatives
      - [D] 02_Static & Mockups
      - [D] 03_Voiceovers
      - [D] 04_Competitor Creatives
    - [D] 05_Lessons & Course Materials
      - [D] 01_Curriculum
      - [D] 02_Lesson Videos
      - [D] 03_Transcripts
      - [D] 04_Workbooks & PDFs
    - [D] 99_Archive
  - [D] GUM - Viral Content Blueprint - Alexander Kurz
    - [D] 00_Brief & Strategy
    - [D] 01_Research & Competitors
    - [D] 02_Landing Page
    - [D] 03_Promo Video
      - [D] 01_Scripts
      - [D] 02_Voiceovers
      - [D] 03_Raw Generations
      - [D] 04_Final Edits
    - [D] 04_Ad Creatives
      - [D] 01_Video Creatives
      - [D] 02_Static & Mockups
      - [D] 03_Voiceovers
      - [D] 04_Competitor Creatives
    - [D] 05_Lessons & Course Materials
      - [D] 01_Curriculum
      - [D] 02_Lesson Videos
      - [D] 03_Transcripts
      - [D] 04_Workbooks & PDFs
    - [D] 99_Archive
  - [D] OLZHAS - AI Content Bootcamp
    - [D] 00_Brief & Strategy
    - [D] 01_Research & Competitors
    - [D] 02_Landing Page
    - [D] 03_Promo Video
      - [D] 01_Scripts
      - [D] 02_Voiceovers
      - [D] 03_Raw Generations
      - [D] 04_Final Edits
    - [D] 04_Ad Creatives
      - [D] 01_Video Creatives
      - [D] 02_Static & Mockups
      - [D] 03_Voiceovers
      - [D] 04_Competitor Creatives
    - [D] 05_Lessons & Course Materials
      - [D] 01_Curriculum
      - [D] 02_Lesson Videos
      - [D] 03_Transcripts
      - [D] 04_Workbooks & PDFs
    - [D] 99_Archive
- [D] 05_Marketing & Creative Library
  - [D] 01_Research & Tools
    - [F] Research on the AI-tools that generate a bunch of creatives for ADS
  - [D] 02_Hooks Vault
    - [F] HOOKS VAULT
  - [D] 03_Production Briefs
    - [F] ТЗ НА МОНТАЖ КРЕО
    - [F] ТЗ на Лендинги
    - [F] ТЗ на монтаж
- [D] 99_Archive
```
