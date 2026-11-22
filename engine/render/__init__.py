"""Single-model rendering package (`pdf.py`, `html.py`, `xlsx.py`).

Frozen public function signatures implemented by Wave 1 · Workstream C:
- `engine.render.pdf.render_weekly_pdfs(model: ReportModel, out_dir: Path) -> dict[str, Path]`
    Returns `{"liquidity_pdf": Path, "cash_flow_pdf": Path}`
- `engine.render.html.render_reports_html(models: list[ReportModel], config: dict[str, Any], workspace_dir: Path) -> Path`
    Writes `<workspace_dir>/Reports.html` and returns its Path.
- `engine.render.html.render_setup_html(config: dict[str, Any], audit_entries: list[dict[str, Any]], workspace_dir: Path) -> Path`
    Writes `<workspace_dir>/Setup.html` and returns its Path.
- `engine.render.xlsx.render_payroll_xlsx(model: ReportModel, out_path: Path) -> Path`
    Writes the 4-tab payroll review workbook (`Runs`, `Tie-Out`, `Checks`, `Exceptions`, plus `Draft Journal`) and returns its Path.
"""
