#!/usr/bin/env python3
import os
import sys
import json
import webbrowser
from pathlib import Path
import redis

def generate_visualization():
    # 1. Kết nối tới Redis (Port 6380 theo compose config)
    redis_port = int(os.getenv("REDIS_PORT", 6380))
    r = redis.Redis(host="localhost", port=redis_port, decode_responses=True)
    
    try:
        r.ping()
    except Exception as e:
        print(f"❌ Không thể kết nối tới Redis trên cổng {redis_port}. Hãy đảm bảo Docker container đang chạy.")
        print(f"   Lỗi: {e}")
        return

    # 2. Quét các keys để lấy Nodes
    tasks = {}
    conditions = {}
    results = {}
    
    # Lấy Tasks
    task_keys = r.keys("kaos:graph:task:*")
    for key in task_keys:
        task_id = key.split(":")[-1]
        data = r.hgetall(key)
        tasks[task_id] = data

    # Lấy Conditions
    cond_keys = r.keys("kaos:graph:condition:*")
    for key in cond_keys:
        cond_id = key.split(":")[-1]
        data = r.hgetall(key)
        conditions[cond_id] = data

    # Lấy Results
    result_keys = r.keys("kaos:graph:result:*")
    for key in result_keys:
        result_id = key.split(":")[-1]
        data = r.hgetall(key)
        results[result_id] = data

    # 3. Quét các Edges
    edges_list = []
    
    # DEPENDS_ON (Task -> Task)
    dep_keys = r.keys("kaos:graph:edge:depends_on:*")
    for key in dep_keys:
        child_id = key.split(":")[-1]
        parents = r.smembers(key)
        for parent_id in parents:
            edges_list.append({
                "from": child_id,
                "to": parent_id,
                "label": "DEPENDS_ON",
                "color": {"color": "#3b82f6", "highlight": "#1d4ed8"},
                "arrows": "to",
                "dashes": True
            })

    # REQUIRES (Task -> Condition)
    req_keys = r.keys("kaos:graph:edge:requires:*")
    for key in req_keys:
        task_id = key.split(":")[-1]
        conds = r.smembers(key)
        for cond_id in conds:
            edges_list.append({
                "from": task_id,
                "to": cond_id,
                "label": "REQUIRES",
                "color": {"color": "#f59e0b", "highlight": "#d97706"},
                "arrows": "to"
            })

    # PRODUCES (Task -> Result)
    prod_keys = r.keys("kaos:graph:edge:produces:*")
    for key in prod_keys:
        task_id = key.split(":")[-1]
        res_list = r.smembers(key)
        for res_id in res_list:
            edges_list.append({
                "from": task_id,
                "to": res_id,
                "label": "PRODUCES",
                "color": {"color": "#10b981", "highlight": "#047857"},
                "arrows": "to"
            })

    # MUTATES (Result -> Condition)
    mut_keys = r.keys("kaos:graph:edge:mutates:*")
    for key in mut_keys:
        res_id = key.split(":")[-1]
        conds = r.smembers(key)
        for cond_id in conds:
            edges_list.append({
                "from": res_id,
                "to": cond_id,
                "label": "MUTATES",
                "color": {"color": "#8b5cf6", "highlight": "#6d28d9"},
                "arrows": "to",
                "dashes": True
            })

    # 4. Định dạng Nodes cho Vis.js
    nodes_list = []
    
    # Add Tasks (Nhân)
    for task_id, data in tasks.items():
        title = data.get("title", "")
        status = data.get("status", "PENDING")
        nodes_list.append({
            "id": task_id,
            "label": f"Task: {task_id}\n[{status}]",
            "title": f"<b>Title:</b> {title}<br><b>Desc:</b> {data.get('description', '')}<br><b>Module:</b> {data.get('module', '')}",
            "color": {
                "background": "#dbeafe" if status == "SUCCESS" else "#fee2e2" if status == "FAILED" else "#f3f4f6",
                "border": "#2563eb" if status == "SUCCESS" else "#dc2626" if status == "FAILED" else "#4b5563"
            },
            "shape": "box",
            "margin": 10,
            "font": {"bold": True}
        })

    # Add Conditions (Duyên)
    for cond_id, data in conditions.items():
        ctype = data.get("type", "unknown")
        content = data.get("content", "")
        # Rút gọn content hiển thị
        short_content = content[:50] + "..." if len(content) > 50 else content
        nodes_list.append({
            "id": cond_id,
            "label": f"Cond: {ctype}\n({cond_id})",
            "title": f"<b>Type:</b> {ctype}<br><b>Content:</b> <pre>{content}</pre>",
            "color": {
                "background": "#ffedd5" if ctype == "feedback" else "#fef9c3",
                "border": "#ea580c" if ctype == "feedback" else "#ca8a04"
            },
            "shape": "ellipse",
            "margin": 8
        })

    # Add Results (Quả)
    for res_id, data in results.items():
        success = data.get("success", "false") == "true"
        attempt = data.get("attempt", "1")
        nodes_list.append({
            "id": res_id,
            "label": f"Result (Att {attempt})\n" + ("✅ OK" if success else "❌ FAIL"),
            "title": f"<b>Success:</b> {success}<br><b>Attempt:</b> {attempt}<br><b>Created:</b> {data.get('files_created', '[]')}<br><b>Modified:</b> {data.get('files_modified', '[]')}<br><b>Error:</b> {data.get('error_message', 'None')}",
            "color": {
                "background": "#d1fae5" if success else "#fee2e2",
                "border": "#059669" if success else "#dc2626"
            },
            "shape": "database",
            "margin": 8
        })

    # 5. Sinh mã HTML chứa thư viện Vis.js để render Đồ thị tương tác
    html_template = f"""<!DOCTYPE html>
<html>
<head>
    <title>KAOS Nhân-Duyên-Quả Graph Visualizer</title>
    <script type="text/javascript" src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
    <style type="text/css">
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            margin: 0;
            padding: 0;
            background-color: #f8fafc;
            display: flex;
            flex-direction: column;
            height: 100vh;
        }}
        #header {{
            background-color: #0f172a;
            color: white;
            padding: 12px 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1);
        }}
        #header h1 {{
            margin: 0;
            font-size: 20px;
            font-weight: 700;
        }}
        #legend {{
            display: flex;
            gap: 15px;
            font-size: 13px;
        }}
        .legend-item {{
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .dot {{
            width: 12px;
            height: 12px;
            border-radius: 3px;
        }}
        #container {{
            flex-grow: 1;
            position: relative;
        }}
        #mynetwork {{
            width: 100%;
            height: 100%;
            border: none;
            background-color: #ffffff;
        }}
        #footer {{
            background-color: #f1f5f9;
            padding: 8px 24px;
            font-size: 12px;
            color: #64748b;
            text-align: center;
            border-top: 1px solid #e2e8f0;
        }}
    </style>
</head>
<body>
    <div id="header">
        <h1>🌌 KAOS Đồ thị Nhân-Duyên-Quả (Knowledge Graph)</h1>
        <div id="legend">
            <div class="legend-item"><span class="dot" style="background-color: #dbeafe; border: 1px solid #2563eb;"></span> <b>Nhân (Task)</b></div>
            <div class="legend-item"><span class="dot" style="background-color: #fef9c3; border: 1px solid #ca8a04;"></span> <b>Duyên (Condition)</b></div>
            <div class="legend-item"><span class="dot" style="background-color: #ffedd5; border: 1px solid #ea580c;"></span> <b>Duyên động (Feedback)</b></div>
            <div class="legend-item"><span class="dot" style="background-color: #d1fae5; border: 1px solid #059669;"></span> <b>Quả (Result OK)</b></div>
            <div class="legend-item"><span class="dot" style="background-color: #fee2e2; border: 1px solid #dc2626;"></span> <b>Quả (Result FAIL)</b></div>
        </div>
    </div>
    
    <div id="container">
        <div id="mynetwork"></div>
    </div>

    <div id="footer">
        Dữ liệu kết nối trực tiếp từ Redis (Port: {redis_port}). Click đúp để zoom, di chuột qua Node để xem chi tiết JSON.
    </div>

    <script type="text/javascript">
        // Data parsed from python
        const nodes = new vis.DataSet({json.dumps(nodes_list)});
        const edges = new vis.DataSet({json.dumps(edges_list)});

        // create a network
        const container = document.getElementById('mynetwork');
        const data = {{
            nodes: nodes,
            edges: edges
        }};
        const options = {{
            nodes: {{
                font: {{
                    size: 13,
                    face: 'sans-serif'
                }}
            }},
            edges: {{
                width: 2,
                font: {{
                    size: 11,
                    align: 'middle'
                }},
                smooth: {{
                    type: 'cubicBezier',
                    forceDirection: 'none',
                    roundness: 0.5
                }}
            }},
            physics: {{
                forceAtlas2Based: {{
                    gravitationalConstant: -50,
                    centralGravity: 0.01,
                    springLength: 100,
                    springConstant: 0.08
                }},
                maxVelocity: 50,
                solver: 'forceAtlas2Based',
                timestep: 0.35,
                stabilization: {{ iterations: 150 }}
            }}
        }};
        const network = new vis.Network(container, data, options);
    </script>
</body>
</html>
"""

    output_path = Path(__file__).parent.parent / "graph_visualizer.html"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_template)
    
    print(f"🎉 Đã sinh file giao diện đồ thị trực quan tại: {output_path.resolve()}")
    print("👉 Hãy mở file này bằng trình duyệt của bạn (hoặc chạy double-click vào file).")
    
    # Tự động mở trình duyệt
    try:
        webbrowser.open(output_path.as_uri())
    except Exception:
        pass

if __name__ == "__main__":
    generate_visualization()
