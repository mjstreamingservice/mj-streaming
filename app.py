import streamlit as st
import urllib.parse

# 1. Configuración básica de la página
st.set_page_config(page_title="MJ Streaming", page_icon="🎬", layout="centered")

st.title("🎬 Catálogo MJ Streaming")
st.write("Selecciona la plataforma que deseas adquirir y contáctanos para activarla de inmediato.")
st.divider()

# 2. Nuestra base de datos de cuentas
cuentas = [
    {"nombre": "Netflix", "tipo": "Pantalla Premium 4K", "precio": "15.000", "disponible": True},
    {"nombre": "Spotify", "tipo": "Cuenta Individual", "precio": "10.000", "disponible": False},
    {"nombre": "Amazon Prime", "tipo": "Pantalla Compartida", "precio": "8.000", "disponible": True}
]

# 3. Dibujar el catálogo en la pantalla usando columnas
col1, col2, col3 = st.columns(3)

# Mostrar Netflix
with col1:
    st.subheader(cuentas[0]["nombre"])
    st.caption(cuentas[0]["tipo"])
    st.write(f"**${cuentas[0]['precio']}**")
    
    if cuentas[0]["disponible"]:
        mensaje = urllib.parse.quote(f"Hola, quiero comprar {cuentas[0]['nombre']} - {cuentas[0]['tipo']} por ${cuentas[0]['precio']}. Aquí envío mi comprobante.")
        link_wa = f"https://wa.me/573000000000?text={mensaje}"
        st.link_button("Pagar y Activar", link_wa)
    else:
        st.button("Agotado", disabled=True, key="btn1")

# Mostrar Spotify
with col2:
    st.subheader(cuentas[1]["nombre"])
    st.caption(cuentas[1]["tipo"])
    st.write(f"**${cuentas[1]['precio']}**")
    
    if cuentas[1]["disponible"]:
        mensaje = urllib.parse.quote(f"Hola, quiero comprar {cuentas[1]['nombre']}.")
        link_wa = f"https://wa.me/573000000000?text={mensaje}"
        st.link_button("Pagar y Activar", link_wa)
    else:
        st.button("Agotado", disabled=True, key="btn2")

# Mostrar Amazon Prime
with col3:
    st.subheader(cuentas[2]["nombre"])
    st.caption(cuentas[2]["tipo"])
    st.write(f"**${cuentas[2]['precio']}**")
    
    if cuentas[2]["disponible"]:
        mensaje = urllib.parse.quote(f"Hola, quiero comprar {cuentas[2]['nombre']} por ${cuentas[2]['precio']}.")
        link_wa = f"https://wa.me/573000000000?text={mensaje}"
        st.link_button("Pagar y Activar", link_wa)
    else:
        st.button("Agotado", disabled=True, key="btn3")