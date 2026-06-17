import streamlit as st
import pandas as pd
import numpy as np
from geopy.geocoders import ArcGIS
from datetime import datetime, date
import unicodedata
import re
import requests
import json
import time
import sqlite3

# ==========================================
# 1. CONFIGURAÇÕES GERAIS E CSS
# ==========================================
st.set_page_config(page_title="Visualizador Operacional", layout="wide")

# Chave do Google Maps
GOOGLE_MAPS_API_KEY = "AIzaSyCU46Uqvxnxkh5dF21jwUxdEtejMwstUC8"
DB_CACHE = 'memoria_geocoding.db'

# --- PALETA DE CORES ---
CORES_HEX = [
    '#e6194b', "#faf61c", "#1a1916", '#3cb44b', '#f58231', 
    "#666968", '#46f0f0', '#f032e6', '#bcf60c', "#0d5224", 
    '#008080', "#580e86", '#9a6324', "#4363d8", '#800000'
]

# --- ESTILO CSS PERSONALIZADO ---
st.markdown("""
<style>
    div.kpi-card {
        padding: 15px;
        border-radius: 10px;
        color: white;
        text-align: center;
        box-shadow: 2px 2px 5px rgba(0,0,0,0.2);
        margin-bottom: 10px;
    }
    div.kpi-title { font-size: 14px; font-weight: bold; margin-bottom: 5px; opacity: 0.9; }
    div.kpi-value { font-size: 28px; font-weight: bold; }
    
    .bg-blue { background-color: #2980b9; }
    .bg-green { background-color: #27ae60; }
    .bg-red { background-color: #c0392b; }
    .bg-orange { background-color: #d35400; }
    
    [data-testid="stDataFrame"] { width: 100%; }
</style>
""", unsafe_allow_html=True)


# ==========================================
# 2. FUNÇÕES AUXILIARES E DE MEMÓRIA (CACHE)
# ==========================================

def limpar_volume(val):
    if pd.isna(val) or val == '' or val is None: return 0.0
    try:
        if isinstance(val, str):
            v_str = re.sub(r'[^\d.,]', '', val).replace(',', '.')
            return float(v_str) if v_str else 0.0
        return float(val)
    except:
        return 0.0

def iniciar_banco_cache():
    conn = sqlite3.connect(DB_CACHE)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cache_geo (
            Chave TEXT PRIMARY KEY,
            Latitude TEXT,
            Longitude TEXT,
            Score TEXT,
            ID_Cliente TEXT,
            End_Correto TEXT,
            Bairro_Correto TEXT
        )
    ''')
    conn.commit()
    conn.close()

def carregar_memoria():
    iniciar_banco_cache()
    conn = sqlite3.connect(DB_CACHE)
    df = pd.read_sql("SELECT * FROM cache_geo", conn)
    conn.close()
    return df

def salvar_memoria(novo_df_cache):
    iniciar_banco_cache()
    colunas_banco = ['Chave', 'Latitude', 'Longitude', 'Score', 'ID_Cliente', 'End_Correto', 'Bairro_Correto']
    for col in colunas_banco:
        if col not in novo_df_cache.columns:
            novo_df_cache[col] = ""
            
    conn = sqlite3.connect(DB_CACHE)
    novo_df_cache.to_sql('temp_geo', conn, if_exists='replace', index=False)
    conn.execute('''
        INSERT OR REPLACE INTO cache_geo (Chave, Latitude, Longitude, Score, ID_Cliente, End_Correto, Bairro_Correto)
        SELECT Chave, Latitude, Longitude, Score, ID_Cliente, End_Correto, Bairro_Correto FROM temp_geo
    ''')
    conn.commit()
    conn.close()

def gerar_chave_cache(endereco, cidade, cep):
    e = str(endereco).strip().lower()
    c = str(cidade).strip().lower()
    z = str(cep).replace('-', '').replace('.', '').strip()
    return f"{e}|{c}|{z}"

def corrigir_texto_erp(texto):
    if not isinstance(texto, str): return ""
    texto = str(texto)
    correcoes = {
        'sÃ£o': 'são', 'SÃ£o': 'São', 'Ã£': 'ã', 'Ã©': 'é', 
        'Ã³': 'ó', 'Ã­': 'í', 'Ã§': 'ç', 'Ãª': 'ê', 
        'Ã¢': 'â', 'Ãµ': 'õ', 'Ãº': 'ú', 'Ã¡': 'á',
        'Ã‰': 'É', 'Ã': 'Á', 'Ã“': 'Ó'
    }
    for errado, certo in correcoes.items():
        texto = texto.replace(errado, certo)
    try: 
        texto = texto.encode('cp1252').decode('utf-8')
    except: 
        pass
    return texto.strip()

def limpar_endereco_para_geocoding(endereco):
    endereco = corrigir_texto_erp(endereco)
    if " - " in endereco:
        partes = endereco.split(" - ")
        if any(char.isdigit() for char in partes[0]): 
            endereco = partes[0]
            
    endereco = re.sub(r'[.,\- ]+$', '', endereco)
    if not isinstance(endereco, str): return ""
    
    try:
        endereco = endereco.encode('cp1252').decode('utf-8')
    except:
        pass 

    padrao_inutil = r'(?i)\b(ap|apt|apto|bl|bloco|sl|sala|cj|conjunto|casa|loja|térreo|fundos|km|frente|lado)\b.*'
    endereco = re.sub(padrao_inutil, '', endereco)
    
    nfkd_form = unicodedata.normalize('NFKD', endereco)
    endereco = "".join([c for c in nfkd_form if not unicodedata.combining(c)])
    endereco = re.sub(r'[^a-zA-Z0-9\s,-]', '', endereco)
    
    if ',' not in endereco:
        endereco = re.sub(r'([a-zA-Z])\s+(\d+)', r'\1, \2', endereco)
        
    return endereco.strip().strip(",.-")

def is_valid_sp_coord(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
        return (-25.5 <= lat <= -19.0) and (-53.5 <= lon <= -44.0)
    except:
        return False

@st.cache_data(ttl=3600)
def get_rota_detalhada_rua_otimizada(lista_pontos):
    if len(lista_pontos) < 2: return []
    caminho_completo = []
    chunk_size = 20
    for i in range(0, len(lista_pontos) - 1, chunk_size - 1):
        chunk = lista_pontos[i : i + chunk_size]
        if len(chunk) < 2: continue
        coords_str = ";".join([f"{p[1]},{p[0]}" for p in chunk])
        url = f"http://router.project-osrm.org/route/v1/driving/{coords_str}?overview=full&geometries=geojson"
        try:
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                data = r.json()
                if 'routes' in data and len(data['routes']) > 0:
                    geometry = data['routes'][0]['geometry']['coordinates']
                    caminho_completo.extend([[p[1], p[0]] for p in geometry])
                    continue
        except: pass
        caminho_completo.extend(chunk)
    return caminho_completo


# ==========================================
# 3. LÓGICA DO VISUALIZADOR OPERACIONAL
# ==========================================

def mostrar_visualizador_operacional():
    st.title("👁️ Visualizador Operacional de Rotas")
    
    st.markdown("""
        <div style="background-color: #fff3cd; padding: 20px; border-radius: 10px; border-left: 6px solid #ffc107; margin-bottom: 20px;">
            <h4 style="color: #856404; margin-top: 0;">⚠️ Atenção: Regra de Importação</h4>
            <p style="color: #856404; font-size: 15px;">
                Para garantir a precisão do mapa, suba <b>apenas clientes que possuam data de visita programada</b>.<br>
                Linhas com datas vazias ou textos como 'Fechamento' serão ignoradas pelo visualizador.
            </p>
        </div>
    """, unsafe_allow_html=True)

    upload_view = st.file_uploader("📂 Subir Arquivo de Rota (CSV/Excel)", type=['csv', 'xlsx'], key="up_visualizador")

    if 'arquivo_atual_vis' not in st.session_state or st.session_state.get('arquivo_atual_vis') != (upload_view.name if upload_view else None):
        if 'df_view_pronto' in st.session_state:
            del st.session_state['df_view_pronto']
        st.session_state['arquivo_atual_vis'] = upload_view.name if upload_view else None

    if upload_view:
        if upload_view.name.endswith('.csv'):
            df_view_raw = pd.read_csv(upload_view)
        else:
            df_view_raw = pd.read_excel(upload_view)

        st.write("### 📊 Dados Carregados")
        st.dataframe(df_view_raw.head(), use_container_width=True)

        st.write("### ⚙️ Mapeamento de Colunas")
        colunas = df_view_raw.columns.tolist()
        
        def tentar_achar_coluna(palavras_chave):
            for col in colunas:
                if any(p.lower() in str(col).lower() for p in palavras_chave):
                    return colunas.index(col)
            return 0

        c1, c2, c3 = st.columns(3)
        col_vendedor = c1.selectbox("Vendedor:", colunas, index=tentar_achar_coluna(['vendedor', 'vend']))
        col_data_orig = c2.selectbox("Data da Visita:", colunas, index=tentar_achar_coluna(['data', 'roteiro']))
        col_id_orig = c3.selectbox("Código do Cliente:", colunas, index=tentar_achar_coluna(['cód', 'cod', 'id', 'sap', 'cnpj']))
        
        c4, c5, c6 = st.columns(3)
        col_end_orig = c4.selectbox("Endereço:", colunas, index=tentar_achar_coluna(['endereço', 'endereco', 'rua']))
        col_bairro_orig = c5.selectbox("Bairro:", colunas, index=tentar_achar_coluna(['bairro']))
        col_nome_orig = c6.selectbox("Nome do Cliente:", colunas, index=tentar_achar_coluna(['cliente', 'razão', 'razao', 'nome', 'social']))

        c7, c8, c9 = st.columns(3)
        col_cidade_orig = c7.selectbox("Cidade/Município:", colunas, index=tentar_achar_coluna(['cidade', 'municipio', 'mun']))
        col_cep_orig = c8.selectbox("CEP (Opcional):", colunas, index=tentar_achar_coluna(['cep']))
        
        idx_vol = tentar_achar_coluna(['vol', 'peso', 'qtde', 'quantidade', 'volume', 'litros'])
        col_vol_orig = c9.selectbox("Volume/Peso (Opcional):", ["(Nenhum)"] + colunas, index=idx_vol+1 if idx_vol else 0)

        # --- NOVA SELEÇÃO: ROTA MATRIZ ---
        st.write("### 📅 Organização Semanal (Opcional)")
        col_rota_matriz = st.selectbox("Coluna de Rota/Semana (Ex: 'Rota Matriz' com dados '1ªSem.1-Segunda'):", ["(Nenhuma)"] + colunas, index=tentar_achar_coluna(['rota matriz', 'semana', 'matriz']))

        st.divider()

        if st.button("🚀 Confirmar Mapeamento e Gerar Mapa", type="primary", use_container_width=True):
            with st.spinner("Estruturando dados e buscando coordenadas no satélite..."):
                df_view = df_view_raw.copy()
                
                data_convertida = pd.to_datetime(df_view[col_data_orig], errors='coerce')
                df_view = df_view[data_convertida.notna()].copy()
                df_view['Roteiro_Data'] = data_convertida[data_convertida.notna()].dt.date
                
                df_view['Codigo_Cliente'] = df_view[col_id_orig].astype(str).str.replace(r'\.0$', '', regex=True)
                df_view['Nome_Vendedor'] = df_view[col_vendedor].astype(str)
                df_view['Cliente'] = df_view[col_nome_orig].astype(str)
                df_view['CEP_Ref'] = df_view[col_cep_orig].astype(str)
                
                df_view['Endereço_Limpo'] = df_view[col_end_orig].astype(str).apply(limpar_endereco_para_geocoding)
                df_view['Bairro'] = df_view[col_bairro_orig].astype(str).apply(corrigir_texto_erp)
                df_view['Cidade_Ref'] = df_view[col_cidade_orig].astype(str).apply(corrigir_texto_erp)
                
                df_view['Volume'] = df_view[col_vol_orig] if col_vol_orig != "(Nenhum)" else 0
                df_view['Vol_Calc'] = df_view['Volume'].apply(limpar_volume)

                # --- EXTRAINDO A SEMANA E O DIA DA COLUNA ROTA MATRIZ ---
                def extrair_semana_dia(val):
                    val = str(val).strip()
                    if val == "S/ Rota" or pd.isna(val) or val == 'nan': 
                        return "Semana Indefinida", "Dia Indefinido"
                    try:
                        if "." in val and "-" in val:
                            partes = val.split('.')
                            sem = partes[0].replace('Sem', ' Semana')
                            dia = partes[1].split('-')[-1].upper()
                            return sem, dia
                        else:
                            return "Semana Única", val.upper()
                    except:
                        return "Semana Única", val.upper()

                if col_rota_matriz != "(Nenhuma)":
                    df_view['Rota_Matriz_Raw'] = df_view[col_rota_matriz]
                    df_view['Semana_Visita'] = df_view['Rota_Matriz_Raw'].apply(lambda x: extrair_semana_dia(x)[0])
                    df_view['Dia_Visita'] = df_view['Rota_Matriz_Raw'].apply(lambda x: extrair_semana_dia(x)[1])
                else:
                    df_view['Semana_Visita'] = "Semana Única"
                    df_view['Dia_Visita'] = "Dia " + df_view['Roteiro_Data'].astype(str)


                df_memoria = carregar_memoria()
                
                if 'ID_Cliente' in df_memoria.columns:
                    mem_by_id = df_memoria.drop_duplicates(subset=['ID_Cliente'], keep='last').set_index('ID_Cliente').to_dict('index')
                else:
                    mem_by_id = {}
                
                df_view['Latitude'] = 0.0
                df_view['Longitude'] = 0.0
                
                for idx, row in df_view.iterrows():
                    cod_cli = row['Codigo_Cliente']
                    if cod_cli in mem_by_id:
                        df_view.at[idx, 'Latitude'] = float(mem_by_id[cod_cli].get('Latitude', 0.0))
                        df_view.at[idx, 'Longitude'] = float(mem_by_id[cod_cli].get('Longitude', 0.0))
                        
                mask_sem_coord = (df_view['Latitude'] == 0.0) | (df_view['Latitude'].isna())
                if mask_sem_coord.any():
                    missing_indices = df_view[mask_sem_coord].index
                    novos_dados_memoria = []
                    geo = ArcGIS(timeout=10)
                    
                    st.caption(f"📡 Geocodificando {len(missing_indices)} endereços novos no Satélite...")
                    prog_bar = st.progress(0)
                    
                    for i, idx in enumerate(missing_indices):
                        row = df_view.loc[idx]
                        
                        mun_limpo = row['Cidade_Ref']
                        bairro_busca = row['Bairro']
                        if bairro_busca.lower() == 'nan': bairro_busca = ""
                        end_limpo = row['Endereço_Limpo']
                        cod_cli = row['Codigo_Cliente']
                        cep_ref = row['CEP_Ref']
                        
                        chave = gerar_chave_cache(end_limpo, mun_limpo, cep_ref)
                        
                        try:
                            if "," in end_limpo: apenas_rua = end_limpo.split(',')[0].strip()
                            else: apenas_rua = re.sub(r'\d+$', '', end_limpo).strip()

                            loc = None
                            tipo_score = 'Erro'

                            loc = geo.geocode(f"{end_limpo}, {bairro_busca}, {mun_limpo}, SP, Brasil")
                            if loc: tipo_score = 'Alta'
                            
                            if not loc:
                                loc = geo.geocode(f"{end_limpo}, {mun_limpo}, SP, Brasil")
                                if loc: tipo_score = 'Alta'
                                
                            if not loc and apenas_rua:
                                loc = geo.geocode(f"{apenas_rua}, {mun_limpo}, SP, Brasil")
                                if loc: tipo_score = 'Média'
                                
                            if not loc:
                                cep_limpo = str(cep_ref).replace('-', '').replace('.', '').strip()
                                if cep_limpo != 'nan' and cep_limpo != '':
                                    loc = geo.geocode(f"{cep_limpo}, {mun_limpo}, SP, Brasil")
                                    if loc: tipo_score = 'CEP'

                            if loc and is_valid_sp_coord(loc.latitude, loc.longitude):
                                lat_new, lon_new = loc.latitude, loc.longitude
                                df_view.at[idx, 'Latitude'] = lat_new
                                df_view.at[idx, 'Longitude'] = lon_new
                                df_view.at[idx, 'Geo_Score'] = tipo_score
                                novos_dados_memoria.append({
                                    'Chave': chave, 'Latitude': lat_new, 'Longitude': lon_new, 'Score': tipo_score,
                                    'ID_Cliente': cod_cli, 'End_Correto': end_limpo, 'Bairro_Correto': bairro_busca
                                })
                            else:
                                df_view.at[idx, 'Latitude'] = -1.0
                                df_view.at[idx, 'Longitude'] = -1.0

                        except Exception as e:
                            df_view.at[idx, 'Latitude'] = -1.0
                            df_view.at[idx, 'Longitude'] = -1.0
                            
                        prog_bar.progress((i + 1) / len(missing_indices))
                        
                    if novos_dados_memoria:
                        salvar_memoria(pd.DataFrame(novos_dados_memoria))

                st.session_state['df_view_pronto'] = df_view
                st.success("✅ Mapa processado e congelado na memória!")
                time.sleep(1)
                st.rerun()

    if 'df_view_pronto' in st.session_state:
        df_final = st.session_state['df_view_pronto']

        st.write("---")
        c_f1, c_f2 = st.columns(2)
        
        vendedores = sorted(df_final['Nome_Vendedor'].unique())
        sel_v = c_f1.selectbox("Filtrar Vendedor:", vendedores)
        df_filtro = df_final[df_final['Nome_Vendedor'] == sel_v].copy()
        
        datas_rota = sorted(df_filtro['Roteiro_Data'].unique())
        sel_d = c_f2.selectbox("Filtrar Mapa no Dia:", ["Ver Tudo"] + list(datas_rota))

        if sel_d != "Ver Tudo":
            df_mapa = df_filtro[df_filtro['Roteiro_Data'] == sel_d].copy()
        else:
            df_mapa = df_filtro.copy()

        st.subheader(f"🗺️ Mapa Operacional: {sel_v} - {sel_d}")
        
        df_mapa = df_mapa[(df_mapa['Latitude'] != 0.0) & (df_mapa['Latitude'] != -1.0) & (df_mapa['Latitude'].notna())].copy()

        if not df_mapa.empty:
            features = []
            pts_rota_osrm = []
            dias_unicos_globais = sorted(df_filtro['Roteiro_Data'].unique())
            posicoes_vistas = {}
            distancia_offset = 0.00015  

            for _, r in df_mapa.iterrows():
                lat_orig, lon_orig = float(r['Latitude']), float(r['Longitude'])
                
                chave_pos = f"{round(lat_orig, 5)}_{round(lon_orig, 5)}"
                if chave_pos in posicoes_vistas:
                    mult = posicoes_vistas[chave_pos]
                    lat = lat_orig + (distancia_offset * mult)
                    lon = lon_orig + (distancia_offset * mult)
                    posicoes_vistas[chave_pos] += 1
                else:
                    lat, lon = lat_orig, lon_orig
                    posicoes_vistas[chave_pos] = 1

                pts_rota_osrm.append([lat_orig, lon_orig])
                
                try:
                    cor_idx = dias_unicos_globais.index(r['Roteiro_Data']) % len(CORES_HEX)
                    cor_bg = CORES_HEX[cor_idx]
                except:
                    cor_bg = "#1a73e8"
                    
                id_cliente_display = str(r['Codigo_Cliente'])
                link_maps_oficial = f"https://www.google.com/maps/search/?api=1&query={lat_orig},{lon_orig}"
                
                pop_html = f"""
                <div style="font-family: Arial, sans-serif; width: 300px; padding: 5px;">
                    <strong style="font-size: 14px; color: {cor_bg};">{r['Cliente']}</strong><br>
                    <span style="font-size: 12px; color: #666;">Cód: {id_cliente_display} | Dia: {r['Roteiro_Data']}</span>
                    <hr style="border: 0; border-top: 1px solid #eee; margin: 8px 0;">
                    <table width="100%" style="font-size: 12px; margin-bottom: 8px;">
                        <tr><td><strong>Endereço:</strong></td><td>{r['Endereço_Limpo']}</td></tr>
                        <tr><td><strong>Bairro:</strong></td><td>{r['Bairro']}</td></tr>
                        <tr><td><strong>Volume:</strong></td><td>{r.get('Volume', '0')}</td></tr>
                    </table>
                    <div style="border-radius: 4px; overflow: hidden; border: 1px solid #ddd; margin-bottom: 10px;">
                        <img loading="lazy" src="https://maps.googleapis.com/maps/api/streetview?size=300x150&location={lat_orig},{lon_orig}&key={GOOGLE_MAPS_API_KEY}" width="300" height="150">
                    </div>
                    <div style="text-align: center;">
                        <a href="{link_maps_oficial}" target="_blank" style="background-color: {cor_bg}; color: white; padding: 8px 15px; text-decoration: none; border-radius: 4px; font-size: 12px; font-weight: bold; display: inline-block; width: 90%;">📍 Ver no Google Maps</a>
                    </div>
                </div>
                """
                features.append({
                    "position": {"lat": lat, "lng": lon},
                    "title": str(r['Cliente']),
                    "color": cor_bg,
                    "label": "•",
                    "content": pop_html
                })

            caminho_osrm_js = "[]"
            cor_linha_osrm = "#1a73e8" 
            
            if len(pts_rota_osrm) > 1 and sel_d != "Ver Tudo":
                try:
                    rota_rua = get_rota_detalhada_rua_otimizada(pts_rota_osrm)
                    coords_finais = rota_rua if rota_rua else pts_rota_osrm
                    lista_dicts = [{"lat": p[0], "lng": p[1]} for p in coords_finais]
                    caminho_osrm_js = json.dumps(lista_dicts)
                    
                    if sel_d in dias_unicos_globais:
                        cor_linha_osrm = CORES_HEX[dias_unicos_globais.index(sel_d) % len(CORES_HEX)]
                except:
                    pass

            todas_lats = [f["position"]["lat"] for f in features]
            todas_lons = [f["position"]["lng"] for f in features]
            center_lat = sum(todas_lats) / len(todas_lats)
            center_lng = sum(todas_lons) / len(todas_lons)

            google_maps_html = f"""
                <!DOCTYPE html>
                <html>
                <head>
                    <style>
                    #map {{ height: 100%; }} html, body {{ height: 100%; margin: 0; padding: 0; }}
                    .gm-style-iw-d {{ overflow: auto !important; max-height: 480px !important; }}
                    .custom-control {{ 
                        background-color: #fff; border: 2px solid #fff; border-radius: 2px; 
                        box-shadow: 0 2px 6px rgba(0,0,0,.3); cursor: pointer; 
                        margin-right: 10px; margin-top: 10px; padding: 5px; text-align: center; 
                    }}
                    .custom-control label {{ color: rgb(25,25,25); font-family: Roboto,Arial,sans-serif; font-size: 13px; cursor: pointer; font-weight: bold; }}
                    </style>
                    <script>
                    const data = {json.dumps(features)};
                    const routeData = {caminho_osrm_js}; 
                    
                    let map, infoWindow, marker_list = [];
                    let routePath = null;
                    
                    function initMap() {{
                        const estiloLimpo = [
                            {{ featureType: "poi", elementType: "all", stylers: [{{ visibility: "off" }}] }},
                            {{ featureType: "transit", elementType: "all", stylers: [{{ visibility: "off" }}] }}
                        ];

                        map = new google.maps.Map(document.getElementById("map"), {{
                        zoom: 12, center: {{ lat: {center_lat}, lng: {center_lng} }},
                        gestureHandling: "greedy",
                        mapTypeControl: true, 
                        mapTypeControlOptions: {{ style: google.maps.MapTypeControlStyle.DROPDOWN_MENU, position: google.maps.ControlPosition.TOP_RIGHT }},
                        styles: estiloLimpo
                        }});
                        
                        infoWindow = new google.maps.InfoWindow();
                        
                        data.forEach((point) => {{
                        const marker = new google.maps.Marker({{
                            position: point.position, map, title: point.title, optimize: true,
                            label: {{ text: point.label || '', color: 'white', fontSize: '11px', fontWeight: 'bold' }},
                            icon: {{ path: google.maps.SymbolPath.CIRCLE, fillColor: point.color, fillOpacity: 0.9, strokeWeight: 1, strokeColor: '#fff', scale: 6 }}
                        }});
                        marker.addListener("click", () => {{ infoWindow.setContent(point.content); infoWindow.open(map, marker); }});
                        marker_list.push(marker);
                        }});

                        if (routeData && routeData.length > 1) {{
                            routePath = new google.maps.Polyline({{
                                path: routeData, geodesic: true, strokeColor: "{cor_linha_osrm}", strokeOpacity: 0.8, strokeWeight: 4,
                            }});
                            routePath.setMap(map);
                        }}
                        
                        if (marker_list.length > 0) {{
                            const bounds = new google.maps.LatLngBounds();
                            marker_list.forEach(marker => bounds.extend(marker.getPosition()));
                            if(routeData && routeData.length > 1){{
                                routeData.forEach(p => bounds.extend(p));
                            }}
                            map.fitBounds(bounds);
                        }}

                        map.addListener('zoom_changed', function() {{
                            let currentZoom = map.getZoom();
                            let newScale = 6; 
                            
                            if (currentZoom >= 15) newScale = 10; 
                            else if (currentZoom >= 13) newScale = 8; 
                            else if (currentZoom <= 10) newScale = 4; 

                            marker_list.forEach(marker => {{
                                let icon = marker.getIcon();
                                icon.scale = newScale;
                                marker.setIcon(icon);
                            }});
                        }});
                        
                        createLabelControl(map, estiloLimpo);
                        createMarkerToggleControl(map);
                    }}
                    
                    function createLabelControl(map, estiloLimpo) {{
                        const controlDiv = document.createElement('div');
                        controlDiv.className = 'custom-control';
                        const checkbox = document.createElement('input');
                        checkbox.type = 'checkbox'; checkbox.id = 'toggle-labels'; checkbox.checked = false; 
                        const label = document.createElement('label');
                        label.htmlFor = 'toggle-labels';
                        label.appendChild(checkbox); label.appendChild(document.createTextNode(' 🏪 Ver Comércios (Google)'));
                        controlDiv.appendChild(label);
                        
                        checkbox.addEventListener('change', function() {{
                            if (this.checked) {{ map.setOptions({{ styles: [] }}); }} 
                            else {{ map.setOptions({{ styles: estiloLimpo }}); }} 
                        }});
                        map.controls[google.maps.ControlPosition.TOP_LEFT].push(controlDiv);
                    }}

                    function createMarkerToggleControl(map) {{
                        const controlDiv = document.createElement('div');
                        controlDiv.className = 'custom-control';
                        const checkbox = document.createElement('input');
                        checkbox.type = 'checkbox'; checkbox.id = 'toggle-markers'; checkbox.checked = true; 
                        const label = document.createElement('label');
                        label.htmlFor = 'toggle-markers';
                        label.appendChild(checkbox); label.appendChild(document.createTextNode(' 📍 Meus Clientes & Rota'));
                        controlDiv.appendChild(label);
                        
                        checkbox.addEventListener('change', function() {{
                            const isVisible = this.checked;
                            marker_list.forEach(marker => marker.setVisible(isVisible)); 
                            if (routePath) {{ routePath.setVisible(isVisible); }}
                        }});
                        map.controls[google.maps.ControlPosition.TOP_LEFT].push(controlDiv);
                    }}
                    
                    window.initMap = initMap;
                    </script>
                </head>
                <body>
                    <div id="map"></div>
                    <script src="https://maps.googleapis.com/maps/api/js?key={GOOGLE_MAPS_API_KEY}&callback=initMap&v=weekly" defer></script>
                </body>
                </html>
            """
            st.components.v1.html(google_maps_html, height=750, scrolling=False)
        else:
            st.warning("Nenhum cliente com coordenada válida encontrado para este filtro.")
            
        # =========================================================
        # NOVO: DASHBOARD INFERIOR (LISTAGEM SEMANAL / DIÁRIA)
        # =========================================================
        st.divider()
        st.markdown(f"### 📋 Resumo Estratégico: {sel_v}")
        
        # O Dashboard usa TODOS os dados do Vendedor, ignorando o filtro de "Dia Unico" do Mapa
        tot_clientes = len(df_filtro)
        tot_cidades = df_filtro['Cidade_Ref'].nunique()
        
        # Placas de KPI
        st.markdown(f"""
        <div style="display: flex; gap: 20px; margin-bottom: 20px;">
            <div style="background-color: white; padding: 15px; border-radius: 8px; box-shadow: 1px 1px 5px rgba(0,0,0,0.1); flex: 1; border-top: 4px solid #1a73e8; text-align: center;">
                <h3 style="margin: 0; color: #00144F; font-size: 28px;">{tot_clientes} <span style="font-size: 14px; color: #666; font-weight: normal;">TOTAL CLIENTES</span></h3>
            </div>
            <div style="background-color: white; padding: 15px; border-radius: 8px; box-shadow: 1px 1px 5px rgba(0,0,0,0.1); flex: 1; border-top: 4px solid #1a73e8; text-align: center;">
                <h3 style="margin: 0; color: #00144F; font-size: 28px;">{tot_cidades} <span style="font-size: 14px; color: #666; font-weight: normal;">TOTAL CIDADES</span></h3>
            </div>
        </div>
        """, unsafe_allow_html=True)
        
        semanas_unicas = sorted([sem for sem in df_filtro['Semana_Visita'].unique() if pd.notna(sem) and sem != ""])
        
        if semanas_unicas:
            # st.tabs gera a barra de navegação estilo botões
            tabs_semanas = st.tabs(semanas_unicas)
            
            for i, tab in enumerate(tabs_semanas):
                with tab:
                    sem_atual = semanas_unicas[i]
                    df_sem = df_filtro[df_filtro['Semana_Visita'] == sem_atual].copy()
                    
                    # Barra de Pesquisa Interativa do Streamlit
                    busca = st.text_input(f"🔍 Buscar cliente ou cidade...", key=f"busca_sem_{i}")
                    if busca:
                        mask_busca = df_sem['Cliente'].str.contains(busca, case=False, na=False) | df_sem['Cidade_Ref'].str.contains(busca, case=False, na=False)
                        df_sem = df_sem[mask_busca]
                        
                    # Lógica para garantir a ordem correta dos dias da semana
                    ordem_dias = {"SEGUNDA": 1, "TERÇA": 2, "QUARTA": 3, "QUINTA": 4, "SEXTA": 5, "SÁBADO": 6, "SABADO": 6, "DOMINGO": 7}
                    df_sem['Ordem_Dia'] = df_sem['Dia_Visita'].map(ordem_dias).fillna(99)
                    dias_unicos = df_sem.sort_values('Ordem_Dia')['Dia_Visita'].unique()
                    
                    for dia in dias_unicos:
                        df_dia = df_sem[df_sem['Dia_Visita'] == dia].copy()
                        qtd_visitas = len(df_dia)
                        cidades_lista = df_dia['Cidade_Ref'].dropna().unique()
                        qtd_cid = len(cidades_lista)
                        
                        # Pegando algumas cidades para aparecerem no título
                        texto_cidades = ", ".join(cidades_lista[:3]) + ("..." if qtd_cid > 3 else "")
                        
                        # Título dinâmico do Expander (a barra retrátil)
                        titulo_barra = f"🗓️ {dia}  |  {qtd_visitas} visitas  |  📍 {qtd_cid} cid. ({texto_cidades})"
                        
                        # st.expander cria a barra azul escuro que abre e fecha
                        with st.expander(titulo_barra):
                            
                            colunas_desejadas = ['Codigo_Cliente', 'Cliente', 'Endereço_Limpo', 'Bairro', 'Cidade_Ref', 'CEP_Ref', 'Roteiro_Data']
                            colunas_presentes = [c for c in colunas_desejadas if c in df_dia.columns]
                            
                            df_exibir = df_dia[colunas_presentes].rename(columns={
                                'Codigo_Cliente': 'Cód.',
                                'Cliente': 'Razão Social',
                                'Endereço_Limpo': 'Endereço',
                                'Cidade_Ref': 'Município',
                                'CEP_Ref': 'CEP',
                                'Roteiro_Data': 'Data da Visita'
                            })
                            
                            # Tabela do Pandas formatada
                            st.dataframe(df_exibir, hide_index=True, use_container_width=True)
                            
# ==========================================
# 4. EXECUÇÃO PRINCIPAL
# ==========================================

if __name__ == "__main__":
    mostrar_visualizador_operacional()