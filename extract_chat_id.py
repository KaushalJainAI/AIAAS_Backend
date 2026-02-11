import sqlite3
import os

db_path = r"c:\Users\91700\Desktop\AIAAS\Agentic-AI\users.db"

try:
    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}")
        exit(1)

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Check tables first
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = cursor.fetchall()
    print(f"Tables found: {tables}")
    
    if ('users',) in tables:
        # Assuming schema from previous user message: chat_id, username
        cursor.execute("SELECT chat_id, username, first_name FROM users LIMIT 5")
        rows = cursor.fetchall()
        print("\nUsers found:")
        for row in rows:
            print(f"Chat ID: {row[0]}, Username: {row[1]}, Name: {row[2]}")
    else:
        print("Table 'users' not found.")
        
    conn.close()

except Exception as e:
    print(f"Error: {e}")
