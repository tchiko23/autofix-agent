# Examples

This folder contains sample files to help you get started.

## issue_analysis_TICKET-1234.txt

A complete realistic ticket analysis showing both the free form description section AND the structured JSON v1.4.4 block. Use to:

- See what a good analysis looks like before writing your own
- Smoke test your bundle install. Copy to analysis/ and run the bundle. The bundle should attempt a patch on QuarterlyReportService.buildSummary.
- Validate your Rovo prompt produces something structurally similar

## config.env.example and secrets.env.example

Templates for the bundle two configuration files. Copy into templates_external_config/, drop the .example suffix, replace the placeholder values with yours.

Reminder: secrets.env must never get committed. The .gitignore at the repo root excludes both templates_external_config/secrets.env and any top level secrets.env.
