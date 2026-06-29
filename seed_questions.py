"""
seed_questions.py — Run this ONCE to create and seed the questions database.

Usage: python3 seed_questions.py
"""
import sqlite3

QUESTIONS = [
    ("Polity", "What is the significance of Article 370, and what changed when it was abrogated in 2019?"),
    ("Polity", "Explain the difference between a Money Bill and a Finance Bill."),
    ("Polity", "What is the basic structure doctrine, and which case established it?"),
    ("Economy", "What is the difference between fiscal deficit and revenue deficit?"),
    ("Economy", "Explain the concept of stagflation and give a real-world example."),
    ("Economy", "What are Non-Performing Assets, and why do they matter for the banking sector?"),
    ("Geography", "Why does the Indian monsoon sometimes fail, and what are its economic consequences?"),
    ("Geography", "Explain the formation of the Himalayas in terms of plate tectonics."),
    ("CurrentAffairs", "What is the significance of India's G20 presidency, and what were its key outcomes?"),
    ("CurrentAffairs", "Explain the basic objectives of the Production Linked Incentive (PLI) scheme."),
    ("Ethics", "A government officer is asked by a senior to bend a rule for a 'good cause.' How should they respond, and why?"),
    ("Ethics", "What is the difference between law and ethics? Can something be legal but unethical?"),
]

def seed_database():
    conn = sqlite3.connect("questions.db")
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            question TEXT NOT NULL,
            asked_count INTEGER DEFAULT 0
        )
    """)

    cursor.execute("SELECT COUNT(*) FROM questions")
    existing_count = cursor.fetchone()[0]

    if existing_count == 0:
        cursor.executemany(
            "INSERT INTO questions (topic, question) VALUES (?, ?)",
            QUESTIONS
        )
        conn.commit()
        print(f"Seeded {len(QUESTIONS)} questions into questions.db")
    else:
        print(f"Database already has {existing_count} questions. Skipping seed.")

    conn.close()

if __name__ == "__main__":
    seed_database()