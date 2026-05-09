from flask import Flask, render_template
from memberlist import members

import random

app = Flask(__name__)

@app.route("/")
def bracket():

    shuffled = members.copy()
    random.shuffle(shuffled)

    bracket_data = {
        "left": shuffled[:32],
        "right": shuffled[32:64]
    }

    return render_template(
        "bracket.html",
        bracket_data=bracket_data
    )

if __name__ == "__main__":
    app.run(debug=True)