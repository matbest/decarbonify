import streamlit as st

st.set_page_config(layout="wide")

st.title("Portfolio Carbon Tool")

st.sidebar.header("Assets")

asset = st.sidebar.selectbox(
    "Select asset",
    ["Heelands Meeting Centre", "Football Field"]
)

col1, col2 = st.columns(2)

with col1:
    st.subheader("Asset Description")
    st.write(f"You selected: {asset}")

with col2:
    st.subheader("Recommendations")
    st.write("No recommendations yet")

st.divider()

prompt = st.chat_input("Ask about the portfolio")

if prompt:
    st.write(f"You asked: {prompt}")
