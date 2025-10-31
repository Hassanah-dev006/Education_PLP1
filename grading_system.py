
import csv
import json
import os
import statistics
from typing import Dict, List, Optional, Tuple

DATA_FOLDER = "grade_data"
os.makedirs(DATA_FOLDER, exist_ok=True)


# --------------------------
# Domain models
# --------------------------
class Student:
    def __init__(self, student_id: str, name: str):
        self.id = student_id
        self.name = name
        self.scores: Dict[str, float] = {}  # assignment_name -> score

    def to_dict(self):
        return {"id": self.id, "name": self.name, "scores": self.scores}


class Assignment:
    def __init__(self, name: str, weight: float, max_score: float = 100.0):
        self.name = name
        self.weight = weight  # between 0 and 1 but not enforced here (validation later)
        self.max_score = max_score

    def to_dict(self):
        return {"name": self.name, "weight": self.weight, "max_score": self.max_score}


class Course:
    def __init__(self, course_code: str, title: str):
        self.course_code = course_code
        self.title = title
        self.students: Dict[str, Student] = {}  # id -> Student
        self.assignments: Dict[str, Assignment] = {}  # name -> Assignment

    # persistence
    def save(self):
        path = os.path.join(DATA_FOLDER, f"{self.course_code}.json")
        payload = {
            "course_code": self.course_code,
            "title": self.title,
            "students": {sid: s.to_dict() for sid, s in self.students.items()},
            "assignments": {aname: a.to_dict() for aname, a in self.assignments.items()},
        }
        with open(path, "w", encoding="utf8") as f:
            json.dump(payload, f, indent=2)
        print(f"[saved] {path}")

    @classmethod
    def load(cls, course_code: str) -> Optional["Course"]:
        path = os.path.join(DATA_FOLDER, f"{course_code}.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf8") as f:
            payload = json.load(f)
        c = Course(payload["course_code"], payload["title"])
        for sid, sd in payload["students"].items():
            s = Student(sd["id"], sd["name"])
            s.scores = sd.get("scores", {})
            c.students[sid] = s
        for aname, ad in payload["assignments"].items():
            a = Assignment(ad["name"], ad["weight"], ad.get("max_score", 100.0))
            c.assignments[aname] = a
        return c

    # operations
    def import_students_from_csv(self, csv_path: str) -> int:
        count = 0
        with open(csv_path, newline="", encoding="utf8") as f:
            reader = csv.reader(f)
            for row in reader:
                if not row: continue
                sid = str(row[0]).strip()
                name = (row[1].strip() if len(row) > 1 else sid)
                if sid in self.students:
                    continue
                self.students[sid] = Student(sid, name)
                count += 1
        self.save()
        return count

    def add_assignment(self, name: str, weight: float, max_score: float = 100.0):
        if name in self.assignments:
            raise ValueError("Assignment with that name already exists.")
        self.assignments[name] = Assignment(name, weight, max_score)
        self.save()

    def enter_grade(self, student_id: str, assignment_name: str, score: float):
        if student_id not in self.students:
            raise KeyError("Student not found.")
        if assignment_name not in self.assignments:
            raise KeyError("Assignment not found.")
        self.students[student_id].scores[assignment_name] = float(score)
        # do not save automatically for batch operations; caller can call save()
        self.save()

    def bulk_import_grades_csv(self, csv_path: str) -> Tuple[int, int]:
        """
        CSV format: id,assignment_name,score
        Or: id,score1,score2,... with header row of assignment names -> we detect both.
        Returns: (rows_processed, rows_failed)
        """
        processed = 0
        failed = 0
        with open(csv_path, newline="", encoding="utf8") as f:
            reader = csv.reader(f)
            rows = list(reader)
            if not rows:
                return 0, 0
            # detect header-based wide format: header contains "id" as first cell
            first = rows[0]
            if first and first[0].strip().lower() == "id":
                headers = [h.strip() for h in first]
                assignment_names = headers[1:]
                for row in rows[1:]:
                    if not row: continue
                    sid = row[0].strip()
                    if sid not in self.students:
                        failed += 1
                        continue
                    for i, aname in enumerate(assignment_names, start=1):
                        if i >= len(row): continue
                        val = row[i].strip()
                        if val == "":
                            continue
                        try:
                            score = float(val)
                        except ValueError:
                            continue
                        if aname not in self.assignments:
                            # create assignment with zero weight placeholder (require manual fix)
                            self.assignments[aname] = Assignment(aname, 0.0)
                        self.students[sid].scores[aname] = score
                    processed += 1
            else:
                # narrow format rows of (id,assignment,score)
                for row in rows:
                    if len(row) < 3:
                        failed += 1
                        continue
                    sid, aname, score_str = row[0].strip(), row[1].strip(), row[2].strip()
                    try:
                        score = float(score_str)
                    except ValueError:
                        failed += 1
                        continue
                    if sid not in self.students:
                        failed += 1
                        continue
                    if aname not in self.assignments:
                        self.assignments[aname] = Assignment(aname, 0.0)
                    self.students[sid].scores[aname] = score
                    processed += 1
        self.save()
        return processed, failed

    def calculate_weighted_scores(self) -> Dict[str, float]:
        """
        Returns dict student_id -> weighted total (0-100 scale)
        If total assignment weights do not sum to 1, scales proportionally.
        """
        # compute total weight
        weights = {name: a.weight for name, a in self.assignments.items()}
        total_weight = sum(weights.values())
        if total_weight == 0:
            total_weight = 1.0  # avoid division by zero, interpret as raw average later
        results: Dict[str, float] = {}
        for sid, student in self.students.items():
            total = 0.0
            for aname, assignment in self.assignments.items():
                score = student.scores.get(aname, 0.0)
                # normalize score to percentage of assignment's max_score
                perc = (score / assignment.max_score) * 100 if assignment.max_score else 0.0
                total += perc * (assignment.weight / total_weight)
            # total is a percent (0-100)
            results[sid] = round(total, 2)
        return results

    def detect_outliers(self, assignment_name: str, threshold_stddev: float = 2.0) -> List[Tuple[str, float]]:
        """
        For a given assignment returns list of (student_id, score) that are > threshold_stddev away from mean.
        """
        scores = []
        for s in self.students.values():
            if assignment_name in s.scores:
                scores.append((s.id, s.scores[assignment_name]))
        if not scores:
            return []
        values = [v for (_, v) in scores]
        if len(values) < 2:
            return []
        mean = statistics.mean(values)
        stdev = statistics.stdev(values)
        outliers = []
        for sid, val in scores:
            if stdev == 0:
                continue
            if abs(val - mean) > threshold_stddev * stdev:
                outliers.append((sid, val))
        return outliers

    # reporting
    def generate_class_report(self, include_letter: bool = True) -> List[Dict]:
        weighted = self.calculate_weighted_scores()
        report = []
        for sid, student in self.students.items():
            row = {
                "id": sid,
                "name": student.name,
                "weighted_total": weighted.get(sid, 0.0),
                "scores": student.scores,
            }
            if include_letter:
                row["letter"] = numeric_to_letter(row["weighted_total"])
            report.append(row)
        return report

    def export_report_csv(self, out_path: str):
        report = self.generate_class_report(include_letter=True)
        # pick all assignment columns
        assignment_names = list(self.assignments.keys())
        with open(out_path, "w", newline="", encoding="utf8") as f:
            writer = csv.writer(f)
            header = ["id", "name"] + assignment_names + ["weighted_total", "letter"]
            writer.writerow(header)
            for r in report:
                row = [r["id"], r["name"]]
                for an in assignment_names:
                    val = r["scores"].get(an, "")
                    row.append(val)
                row.append(r["weighted_total"])
                row.append(r["letter"])
                writer.writerow(row)
        print(f"[exported CSV] {out_path}")

    def export_report_text(self, out_path: str):
        report = self.generate_class_report(include_letter=True)
        with open(out_path, "w", encoding="utf8") as f:
            f.write(f"Course: {self.course_code} - {self.title}\n")
            f.write("=" * 60 + "\n")
            for r in report:
                f.write(f"{r['id']} | {r['name']} | Total: {r['weighted_total']} | Letter: {r['letter']}\n")
                f.write("  Scores:\n")
                for an, sc in r["scores"].items():
                    f.write(f"    {an}: {sc}\n")
                f.write("-" * 60 + "\n")
        print(f"[exported text] {out_path}")

    def export_report_pdf(self, out_path: str):
        """Optional PDF export using fpdf library. If fpdf is not installed, instruct user."""
        try:
            from fpdf import FPDF
        except Exception:
            print("PDF export requires the 'fpdf' package. Install with: pip install fpdf")
            return
        report = self.generate_class_report(include_letter=True)
        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()
        pdf.set_font("Arial", size=12)
        pdf.cell(0, 8, f"Course: {self.course_code} - {self.title}", ln=True)
        pdf.ln(4)
        for r in report:
            pdf.set_font("Arial", "B", 11)
            pdf.cell(0, 6, f"{r['id']} - {r['name']} | Total: {r['weighted_total']} | Letter: {r['letter']}", ln=True)
            pdf.set_font("Arial", size=10)
            for an, sc in r["scores"].items():
                pdf.cell(0, 5, f"  {an}: {sc}", ln=True)
            pdf.ln(2)
        pdf.output(out_path)
        print(f"[exported PDF] {out_path}")


# --------------------------
# Utilities
# --------------------------
def numeric_to_letter(score: float) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


def list_saved_courses() -> List[str]:
    files = [f[:-5] for f in os.listdir(DATA_FOLDER) if f.endswith(".json")]
    return files


# --------------------------
# CLI (simple)
# --------------------------
def prompt_course() -> Course:
    print("Saved courses:", list_saved_courses() or "(none)")
    choice = input("Load existing course by code or type new code: ").strip()
    if choice == "":
        raise SystemExit("No course selected.")
    existing = Course.load(choice)
    if existing:
        print(f"Loaded course: {existing.course_code} - {existing.title}")
        return existing
    title = input("Enter course title: ").strip() or "Untitled Course"
    new_course = Course(choice, title)
    new_course.save()
    return new_course


def interactive_menu():
    print("=== Python Grading System ===")
    course = prompt_course()
    while True:
        print("\nMenu:")
        print("1) Import students from CSV (id,name)")
        print("2) Add assignment (name,weight,max_score)")
        print("3) Enter grade (student_id,assignment,score)")
        print("4) Bulk import grades from CSV")
        print("5) Calculate weighted totals & show summary")
        print("6) Detect outliers for an assignment")
        print("7) Export report (CSV/text/pdf)")
        print("8) Show students & assignments")
        print("9) Exit")
        choice = input("Choice> ").strip()
        try:
            if choice == "1":
                path = input("CSV path: ").strip()
                count = course.import_students_from_csv(path)
                print(f"Imported {count} students.")
            elif choice == "2":
                name = input("Assignment name: ").strip()
                weight = float(input("Weight (e.g., 0.2 for 20%): ").strip())
                max_score = float(input("Max score (default 100): ").strip() or "100")
                course.add_assignment(name, weight, max_score)
                print("Assignment added.")
            elif choice == "3":
                sid = input("Student ID: ").strip()
                an = input("Assignment name: ").strip()
                sc = float(input("Score: ").strip())
                course.enter_grade(sid, an, sc)
                print("Grade recorded.")
            elif choice == "4":
                path = input("CSV path (id,assignment,score OR id,score1,score2 with header): ").strip()
                processed, failed = course.bulk_import_grades_csv(path)
                print(f"Processed: {processed}, Failed: {failed}")
            elif choice == "5":
                totals = course.calculate_weighted_scores()
                print("Weighted totals:")
                for sid, tot in totals.items():
                    print(f"  {sid} | {course.students[sid].name} | {tot} | {numeric_to_letter(tot)}")
            elif choice == "6":
                an = input("Assignment name: ").strip()
                out = course.detect_outliers(an)
                if not out:
                    print("No outliers or not enough data.")
                else:
                    print("Outliers:")
                    for sid, sc in out:
                        print(f"  {sid} | {course.students[sid].name} : {sc}")
            elif choice == "7":
                fmt = input("Format (csv/text/pdf): ").strip().lower()
                outname = input("Output filename (no folder): ").strip()
                outpath = os.path.join(DATA_FOLDER, outname)
                if fmt == "csv":
                    if not outpath.endswith(".csv"): outpath += ".csv"
                    course.export_report_csv(outpath)
                elif fmt == "text":
                    if not outpath.endswith(".txt"): outpath += ".txt"
                    course.export_report_text(outpath)
                elif fmt == "pdf":
                    if not outpath.endswith(".pdf"): outpath += ".pdf"
                    course.export_report_pdf(outpath)
                else:
                    print("Unknown format.")
            elif choice == "8":
                print("Students:")
                for s in course.students.values():
                    print(f"  {s.id} | {s.name}")
                print("Assignments:")
                for a in course.assignments.values():
                    print(f"  {a.name} | weight={a.weight} | max={a.max_score}")
            elif choice == "9":
                print("Goodbye.")
                break
            else:
                print("Unknown choice.")
        except Exception as e:
            print("[ERROR]", type(e).__name__, e)


# --------------------------
# Example small demo when run directly
# --------------------------
def demo_setup_example(course_code: str = "demo101"):
    """Creates a demo course with a few students/assignments for quick testing."""
    c = Course(course_code, "Demo Course")
    # add students
    c.students["s1"] = Student("s1", "Alice")
    c.students["s2"] = Student("s2", "Bob")
    c.students["s3"] = Student("s3", "Charlie")
    # add assignments
    c.assignments["HW1"] = Assignment("HW1", weight=0.3, max_score=20)
    c.assignments["Exam"] = Assignment("Exam", weight=0.7, max_score=100)
    # add some scores
    c.students["s1"].scores = {"HW1": 18, "Exam": 85}
    c.students["s2"].scores = {"HW1": 12, "Exam": 70}
    c.students["s3"].scores = {"HW1": 19, "Exam": 95}
    c.save()
    print("Demo course created.")


if __name__ == "__main__":
    print("grading_system.py — run in interactive mode or create demo data.")
    print("Type 'demo' to create demo course, or press Enter to open interactive menu.")
    start = input("Start> ").strip().lower()
    if start == "demo":
        demo_setup_example()
    else:
        interactive_menu()
