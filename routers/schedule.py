from __future__ import annotations

import csv
from datetime import date, timedelta
from html import escape
from io import StringIO

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

import crud
import models
import schemas
from database import get_db
from dependencies import require_permission
from services import pdf_utils


router = APIRouter(
    prefix="/schedule",
    tags=["schedule"],
)


def _to_schedule_shift_read(shift: models.ScheduleShift) -> schemas.ScheduleShiftRead:
    return schemas.ScheduleShiftRead(
        id=shift.id,
        week_id=shift.week_id,
        employee_id=shift.employee_id,
        shift_date=shift.shift_date,
        start_time=shift.start_time,
        end_time=shift.end_time,
        break_minutes=shift.break_minutes or 0,
        position=shift.position,
        color=shift.color,
        note=shift.note,
        is_time_off=bool(shift.is_time_off),
        source_template_id=shift.source_template_id,
        created_at=shift.created_at,
        updated_at=shift.updated_at,
        total_hours=crud.schedule_shift_total_hours(shift),
    )


def _normalize_week_start(value: date | None) -> date:
    base = value or date.today()
    return base - timedelta(days=base.weekday())


@router.get("/weeks", response_model=schemas.ScheduleWeekView)
def get_schedule_week_view(
    week_start: date | None = Query(default=None),
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("schedule.view")),
):
    return crud.get_schedule_week_view(db, _normalize_week_start(week_start))


@router.post("/weeks", response_model=schemas.ScheduleWeekRead, status_code=201)
def create_or_get_schedule_week(
    payload: schemas.ScheduleWeekCreate,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("schedule.manage")),
):
    return crud.get_or_create_schedule_week(
        db,
        payload.week_start,
        notes=payload.notes,
    )


@router.put("/weeks/{week_id}/publish", response_model=schemas.ScheduleWeekRead)
def publish_schedule_week(
    week_id: int,
    payload: schemas.ScheduleWeekPublishRequest,
    db: Session = Depends(get_db),
    user: models.PosUser = Depends(require_permission("schedule.publish")),
):
    week = crud.get_schedule_week(db, week_id)
    if not week:
        raise HTTPException(status_code=404, detail="Semana no encontrada")
    return crud.publish_schedule_week(
        db,
        week,
        published_by_user_id=user.id,
        notes=payload.notes,
    )


@router.get("/templates", response_model=list[schemas.ScheduleTemplateRead])
def list_schedule_templates(
    include_inactive: bool = True,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("schedule.view")),
):
    return crud.list_schedule_templates(db, include_inactive=include_inactive)


@router.post("/templates", response_model=schemas.ScheduleTemplateRead, status_code=201)
def create_schedule_template(
    payload: schemas.ScheduleTemplateCreate,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("schedule.manage")),
):
    return crud.create_schedule_template(db, payload)


@router.patch("/templates/{template_id}", response_model=schemas.ScheduleTemplateRead)
def update_schedule_template(
    template_id: int,
    payload: schemas.ScheduleTemplateUpdate,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("schedule.manage")),
):
    template = crud.get_schedule_template(db, template_id)
    if not template:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada")
    return crud.update_schedule_template(db, template, payload)


@router.post("/shifts", response_model=schemas.ScheduleShiftRead, status_code=201)
def upsert_schedule_shift(
    payload: schemas.ScheduleShiftUpsertRequest,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("schedule.manage")),
):
    try:
        shift = crud.upsert_schedule_shift(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_schedule_shift_read(shift)


@router.patch("/shifts/{shift_id}", response_model=schemas.ScheduleShiftRead)
def update_schedule_shift(
    shift_id: int,
    payload: schemas.ScheduleShiftUpdate,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("schedule.manage")),
):
    shift = crud.get_schedule_shift(db, shift_id)
    if not shift:
        raise HTTPException(status_code=404, detail="Turno no encontrado")
    try:
        shift = crud.update_schedule_shift(db, shift, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_schedule_shift_read(shift)


@router.delete("/shifts/{shift_id}", status_code=204)
def delete_schedule_shift(
    shift_id: int,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("schedule.manage")),
):
    shift = crud.get_schedule_shift(db, shift_id)
    if not shift:
        raise HTTPException(status_code=404, detail="Turno no encontrado")
    crud.delete_schedule_shift(db, shift)
    return None


@router.get("/weeks/{week_id}/export.csv")
def export_schedule_csv(
    week_id: int,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("schedule.export")),
):
    week = crud.get_schedule_week(db, week_id)
    if not week:
        raise HTTPException(status_code=404, detail="Semana no encontrada")
    view = crud.get_schedule_week_view(db, week.week_start)
    shift_map = {(shift.employee_id, shift.shift_date): shift for shift in view.shifts}
    week_days = [view.week.week_start + timedelta(days=offset) for offset in range(7)]

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["Empleado", *[day.isoformat() for day in week_days], "Horas semana"]
    )
    for employee in view.employees:
        row_total = 0.0
        row = [employee.name]
        for day in week_days:
            shift = shift_map.get((employee.id, day))
            if not shift:
                row.append("")
                continue
            if shift.is_time_off:
                row.append("Día libre")
                continue
            row.append(f"{shift.start_time or '--'}-{shift.end_time or '--'}")
            row_total += float(shift.total_hours or 0.0)
        row.append(f"{row_total:.2f}")
        writer.writerow(row)

    writer.writerow([])
    writer.writerow(["Totales por día"])
    for day_total in view.day_totals:
        writer.writerow([day_total.shift_date.isoformat(), f"{day_total.total_hours:.2f}"])
    writer.writerow(["Total semana", f"{view.week_total_hours:.2f}"])

    output.seek(0)
    filename = f"horario_{view.week.week_start.isoformat()}.csv"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )


@router.get("/weeks/{week_id}/export.pdf")
def export_schedule_pdf(
    week_id: int,
    db: Session = Depends(get_db),
    _: models.PosUser = Depends(require_permission("schedule.export")),
):
    week = crud.get_schedule_week(db, week_id)
    if not week:
        raise HTTPException(status_code=404, detail="Semana no encontrada")
    view = crud.get_schedule_week_view(db, week.week_start)
    shift_map = {(shift.employee_id, shift.shift_date): shift for shift in view.shifts}
    week_days = [view.week.week_start + timedelta(days=offset) for offset in range(7)]

    start_label = view.week.week_start.strftime("%d/%m/%Y")
    end_label = (view.week.week_start + timedelta(days=6)).strftime("%d/%m/%Y")
    generated_label = date.today().strftime("%d/%m/%Y")

    def _to_ampm(time_str: str | None) -> str:
        if not time_str:
            return "--"
        try:
            hour_str, minute_str = time_str.split(":")
            hour = int(hour_str)
            minute = int(minute_str)
            suffix = "pm" if hour >= 12 else "am"
            hour12 = hour % 12 or 12
            return f"{hour12}:{minute:02d}{suffix}"
        except Exception:
            return time_str

    rows_html: list[str] = []
    for employee in view.employees:
        cells: list[str] = []
        for day in week_days:
            shift = shift_map.get((employee.id, day))
            if not shift:
                cells.append('<td class="cell empty">-</td>')
                continue
            if shift.is_time_off:
                cells.append('<td class="cell off">Día libre</td>')
                continue
            start = _to_ampm(shift.start_time)
            end = _to_ampm(shift.end_time)
            cells.append(
                '<td class="cell">'
                f'<div class="slot">{escape(start)} - {escape(end)}</div>'
                "</td>"
            )
        status_class = "active" if employee.status == "Activo" else "inactive"
        rows_html.append(
            "<tr>"
            f'<td class="employee"><div class="name">{escape(employee.name)}</div>'
            f'<div class="status {status_class}">{escape(employee.status)}</div></td>'
            + "".join(cells)
            + "</tr>"
        )

    headers_html = "".join(
        f'<th>{escape(day.strftime("%a %d").upper())}</th>' for day in week_days
    )

    html_content = f"""
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8" />
        <style>
          @page {{
            size: A4 landscape;
            margin: 14mm 10mm 12mm;
          }}
          body {{
            font-family: Arial, sans-serif;
            color: #0f172a;
            margin: 0;
          }}
          .header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-end;
            border-bottom: 2px solid #0ea5a4;
            padding-bottom: 8px;
            margin-bottom: 10px;
          }}
          .title {{
            font-size: 20px;
            font-weight: 800;
            letter-spacing: .02em;
            margin: 0;
          }}
          .meta {{
            margin-top: 3px;
            font-size: 11px;
            color: #334155;
          }}
          .meta-right {{
            text-align: right;
            font-size: 10px;
            color: #64748b;
          }}
          table {{
            width: 100%;
            border-collapse: collapse;
            table-layout: fixed;
          }}
          thead th {{
            background: #f1f5f9;
            border: 1px solid #cbd5e1;
            padding: 7px 6px;
            font-size: 11px;
            letter-spacing: .03em;
            text-transform: uppercase;
            color: #334155;
          }}
          th:first-child {{
            text-align: left;
            width: 18%;
          }}
          td {{
            border: 1px solid #cbd5e1;
            vertical-align: middle;
            text-align: center;
            height: 34px;
            padding: 4px;
            font-size: 10px;
          }}
          .employee {{
            text-align: left;
            padding: 6px 8px;
            background: #f8fafc;
          }}
          .name {{
            font-weight: 700;
            font-size: 11px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
          }}
          .status {{
            margin-top: 2px;
            font-size: 9px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: .02em;
          }}
          .status.active {{ color: #047857; }}
          .status.inactive {{ color: #b91c1c; }}
          .cell.empty {{ color: #94a3b8; }}
          .cell.off {{
            background: #fef3c7;
            color: #92400e;
            font-weight: 700;
          }}
          .slot {{
            display: inline-block;
            border-radius: 999px;
            padding: 3px 8px;
            background: #0ea5a4;
            color: #f8fafc;
            font-size: 10px;
            font-weight: 700;
            letter-spacing: .01em;
            white-space: nowrap;
          }}
        </style>
      </head>
      <body>
        <div class="header">
          <div>
            <h1 class="title">Horario Semanal</h1>
            <div class="meta">Rango: {start_label} al {end_label}</div>
          </div>
          <div class="meta-right">Generado: {generated_label}</div>
        </div>
        <table>
          <thead>
            <tr>
              <th>Empleado</th>
              {headers_html}
            </tr>
          </thead>
          <tbody>
            {"".join(rows_html)}
          </tbody>
        </table>
      </body>
    </html>
    """

    pdf_bytes = pdf_utils.build_pdf_from_html("Horario semanal", html_content)
    filename = f"horario_{view.week.week_start.isoformat()}_a_{(view.week.week_start + timedelta(days=6)).isoformat()}.pdf"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers=headers,
    )
