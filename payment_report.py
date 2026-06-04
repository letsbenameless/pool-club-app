import datetime as dt
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


SEMESTER_START_DATE = dt.date(2026, 7, 21)


def clean_date(value):
    try:
        return dt.date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return dt.date.today()


def competition_week(comp_date):
    delta_days = (comp_date - SEMESTER_START_DATE).days
    if delta_days < 0:
        return 0

    return delta_days // 7 + 1


def competition_week_label(comp_date):
    week = competition_week(comp_date)
    return f"Week {week}" if week > 0 else "Pre-semester"


def format_comp_date(comp_date):
    return comp_date.strftime("%d/%m/%y")


def money(value):
    amount = float(value or 0)
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):.2f}"


def report_table(data, col_widths, table_body):
    table_data = [
        [Paragraph(str(cell).replace("\n", "<br/>"), table_body) for cell in row]
        for row in data
    ]
    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.black),
        ("LINEBELOW", (0, 0), (-1, -1), 0.65, colors.black),
        ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def player_payment_rows(summary):
    rows = [["Player", "Paid", "Fee", "Status"]]

    if not summary.get("players"):
        return rows + [["No players", "-", "-", "-"]]

    for player in summary.get("players", []):
        rows.append([
            player.get("name", ""),
            "Paid" if player.get("paid") else "Unpaid",
            money(player.get("fee")),
            "Member" if player.get("is_member") else "Non-member",
        ])

    return rows


def membership_counts(summary):
    counts = {
        "members": {"total": 0, "paid": 0, "unpaid": 0},
        "non_members": {"total": 0, "paid": 0, "unpaid": 0},
    }

    for player in summary.get("players", []):
        bucket = counts["members"] if player.get("is_member") else counts["non_members"]
        bucket["total"] += 1

        if player.get("paid"):
            bucket["paid"] += 1
        else:
            bucket["unpaid"] += 1

    return counts


def winner_history_rows(winner_grid):
    rows = [["Week", "1st", "2nd", "3rd"]]

    if not winner_grid:
        return rows + [["No history", "-", "-", "-"]]

    for week in winner_grid:
        comp_date = clean_date(week.get("comp_date"))
        label = f"{competition_week_label(comp_date)}\n{format_comp_date(comp_date)}"
        row = [label]

        for place in (1, 2, 3):
            result = week["places"][place]
            if result.get("winner_name"):
                note = f"\n{result['half_reason']}" if result.get("half_reason") else ""
                row.append(
                    f"{result['winner_name']}\n{money(result['adjusted_winnings'])}{note}"
                )
            else:
                row.append("-")

        rows.append(row)

    return rows


def profit_loss_history_rows(snapshots):
    rows = [["Week", "Date", "Week Profit/Loss", "Year Total"]]

    if not snapshots:
        return rows + [["No history", "-", "-", "-"]]

    yearly_totals = {}
    sorted_snapshots = sorted(
        snapshots,
        key=lambda row: (row.get("year_key", ""), row.get("comp_date", ""))
    )

    for snapshot in sorted_snapshots:
        comp_date = clean_date(snapshot.get("comp_date"))
        year_key = snapshot.get("year_key") or str(comp_date.year)
        weekly_profit = float(snapshot.get("profit_loss") or 0)
        yearly_totals[year_key] = round(yearly_totals.get(year_key, 0.0) + weekly_profit, 2)
        rows.append([
            competition_week_label(comp_date),
            format_comp_date(comp_date),
            money(weekly_profit),
            money(yearly_totals[year_key]),
        ])

    return rows


def build_payment_report_pdf(summary, history, comp_date_value=None):
    comp_date = clean_date(comp_date_value)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
        topMargin=0.45 * inch,
        bottomMargin=0.45 * inch,
    )
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "PoolBody",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        leading=13,
        textColor=colors.black,
        spaceAfter=3,
    )
    heading = ParagraphStyle(
        "PoolHeading",
        parent=body,
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=14,
        textColor=colors.black,
        spaceBefore=8,
        spaceAfter=4,
    )
    table_body = ParagraphStyle(
        "PoolTableBody",
        parent=body,
        fontSize=8,
        leading=10,
        textColor=colors.black,
    )
    story = []
    counts = membership_counts(summary)

    story.append(Paragraph(
        f"Competition Date: {format_comp_date(comp_date)} ({competition_week_label(comp_date)})",
        heading,
    ))
    story.append(Paragraph(
        "Members: {total} | Paid: {paid} | Unpaid: {unpaid}".format(**counts["members"]),
        body,
    ))
    story.append(Paragraph(
        "Non-Members: {total} | Paid: {paid} | Unpaid: {unpaid}".format(
            **counts["non_members"]
        ),
        body,
    ))

    story.append(Spacer(1, 8))
    story.append(Paragraph("Profit/Loss History:", heading))
    story.append(report_table(
        profit_loss_history_rows(history.get("snapshots", [])),
        [1.15 * inch, 1.15 * inch, 2.05 * inch, 2.05 * inch],
        table_body,
    ))

    story.append(Spacer(1, 8))
    story.append(Paragraph("Winner History:", heading))
    story.append(report_table(
        winner_history_rows(history.get("winner_grid", [])),
        [1.05 * inch, 1.78 * inch, 1.78 * inch, 1.78 * inch],
        table_body,
    ))

    story.append(Spacer(1, 8))
    story.append(Paragraph("Player Payments:", heading))
    story.append(report_table(
        player_payment_rows(summary),
        [2.6 * inch, 1.15 * inch, 1.05 * inch, 1.6 * inch],
        table_body,
    ))

    doc.build(story)
    return buffer.getvalue()
