# Taiwan Stock Dashboard

Standalone Streamlit dashboard for Taiwan stock prices, broker flows, foreign investor tracking, stock futures, market futures, and material information.

Run with: `streamlit run app.py`

The foreign-investor tab keeps candidates that appeared during the latest five
trading days. It compares the latest official foreign-investor flow with the
latest five days of tracked foreign-broker branches and labels each candidate
as hold/watch, watch, reduce/exit watch, or insufficient data. The labels are
risk-discipline aids rather than return guarantees.
