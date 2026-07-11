import os, json, requests
from flask import Flask, request, jsonify

app = Flask(__name__, static_folder='../static', static_url_path='/')

API_KEY = os.environ.get("GROQ_API_KEY", "")
API_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "llama-3.3-70b-versatile"

@app.route('/')
def index():
    return app.send_static_file('index.html')

@app.route('/api/ask', methods=['POST'])
def ask():
    try:
        q = request.json.get('question','')
        if not API_KEY:
            return jsonify({'response':'Error: API key not set'})
        
        r = requests.post(API_URL,
            headers={"Authorization":f"Bearer {API_KEY}","Content-Type":"application/json"},
            json={"model":MODEL,"messages":[{"role":"user","content":q}],"temperature":1.0,"max_tokens":4000},
            timeout=60)
        
        if r.status_code == 200:
            resp = r.json()["choices"][0]["message"]["content"]
        else:
            resp = f"Error {r.status_code}"
        
        return jsonify({'response':resp})
    except Exception as e:
        return jsonify({'response':f'Error: {str(e)}'})
