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
    task_keys = r.keys("kg:task:*")
    for key in task_keys:
        task_id = key.split(":")[-1]
        data = r.hgetall(key)
        tasks[task_id] = data

    # Lấy Conditions
    cond_keys = r.keys("kg:condition:*")
    for key in cond_keys:
        cond_id = key.split(":")[-1]
        data = r.hgetall(key)
        conditions[cond_id] = data

    # Lấy Results
    result_keys = r.keys("kg:result:*")
    for key in result_keys:
        result_id = key.split(":")[-1]
        data = r.hgetall(key)
        results[result_id] = data

    # 3. Quét các Edges
    raw_edges = []
    
    # DEPENDS_ON (Task -> Task)
    dep_keys = r.keys("kg:edge:*:depends_on")
    for key in dep_keys:
        child_id = key.split(":")[-2]
        parents = r.smembers(key)
        for parent_id in parents:
            raw_edges.append({
                "type": "depends_on",
                "task": child_id,
                "parent": parent_id
            })

    # REQUIRES (Task -> Condition)
    req_keys = r.keys("kg:edge:*:requires")
    for key in req_keys:
        task_id = key.split(":")[-2]
        conds = r.smembers(key)
        for cond_id in conds:
            raw_edges.append({
                "type": "requires",
                "task": task_id,
                "condition": cond_id
            })

    # PRODUCES (Task -> Result)
    prod_keys = r.keys("kg:edge:*:produces")
    for key in prod_keys:
        task_id = key.split(":")[-2]
        res_list = r.smembers(key)
        for res_id in res_list:
            raw_edges.append({
                "type": "produces",
                "task": task_id,
                "result": res_id
            })

    # MUTATES (Result -> Condition)
    mut_keys = r.keys("kg:edge:*:mutates")
    for key in mut_keys:
        res_id = key.split(":")[-2]
        conds = r.smembers(key)
        for cond_id in conds:
            raw_edges.append({
                "type": "mutates",
                "result": res_id,
                "condition": cond_id
            })

    # 4. Định dạng Nodes cho Vis.js
    nodes_list = []
    
    # Add Tasks
    for task_id, data in tasks.items():
        title = data.get("title", "")
        status = data.get("status", "PENDING")
        # Phân tầng mức (level) để dùng Hierarchical layout nếu cần
        # Lấy từ dependencies để gán level tạm thời
        nodes_list.append({
            "id": task_id,
            "label": f"Task: {task_id}\n[{status}]",
            "group": "task",
            "status": status,
            "title": f"Task: {task_id}",
            "raw_data": data,
            "color": {
                "background": "#dbeafe" if status == "SUCCESS" else "#fee2e2" if status == "FAILED" else "#f3f4f6",
                "border": "#2563eb" if status == "SUCCESS" else "#dc2626" if status == "FAILED" else "#4b5563"
            },
            "shape": "box",
            "margin": 10,
            "font": {"bold": True}
        })

    # Add Conditions
    for cond_id, data in conditions.items():
        ctype = data.get("type", "unknown")
        nodes_list.append({
            "id": cond_id,
            "label": f"Cond: {ctype}\n({cond_id})",
            "group": "condition",
            "cond_type": ctype,
            "title": f"Condition: {cond_id}",
            "raw_data": data,
            "color": {
                "background": "#ffedd5" if ctype == "feedback" else "#fef9c3",
                "border": "#ea580c" if ctype == "feedback" else "#ca8a04"
            },
            "shape": "ellipse",
            "margin": 8
        })

    # Add Results
    for res_id, data in results.items():
        success = data.get("success", "false") == "true"
        attempt = data.get("attempt", "1")
        nodes_list.append({
            "id": res_id,
            "label": f"Result (Att {attempt})\n" + ("✅ OK" if success else "❌ FAIL"),
            "group": "result",
            "success": success,
            "title": f"Result: {res_id}",
            "raw_data": data,
            "color": {
                "background": "#d1fae5" if success else "#fee2e2",
                "border": "#059669" if success else "#dc2626"
            },
            "shape": "database",
            "margin": 8
        })

    # 5. Sinh mã HTML chứa Vis.js Đồ thị Tương tác & Điều khiển Động
    html_template = """<!DOCTYPE html>
<html>
<head>
    <title>KAOS Nhân-Duyên-Quả Graph Visualizer</title>
    <script type="text/javascript" src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
    <style type="text/css">
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            margin: 0;
            padding: 0;
            background-color: #f8fafc;
            display: flex;
            flex-direction: column;
            height: 100vh;
            overflow: hidden;
        }
        #header {
            background-color: #0f172a;
            color: white;
            padding: 12px 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1);
            z-index: 10;
        }
        #header h1 {
            margin: 0;
            font-size: 20px;
            font-weight: 700;
        }
        #controls-wrapper {
            display: flex;
            align-items: center;
            gap: 15px;
        }
        .control-element {
            background-color: #1e293b;
            color: white;
            border: 1px solid #475569;
            padding: 6px 12px;
            border-radius: 4px;
            font-size: 13px;
            cursor: pointer;
            outline: none;
        }
        .control-element:hover {
            background-color: #334155;
        }
        #search-box {
            background-color: #1e293b;
            color: white;
            border: 1px solid #475569;
            padding: 6px 12px;
            border-radius: 4px;
            font-size: 13px;
            width: 150px;
        }
        #main-body {
            display: flex;
            flex-grow: 1;
            position: relative;
            height: calc(100vh - 60px);
        }
        #network-container {
            flex-grow: 1;
            height: 100%;
            position: relative;
            background-color: #ffffff;
        }
        #mynetwork {
            width: 100%;
            height: 100%;
        }
        #sidebar {
            width: 380px;
            background-color: #ffffff;
            border-left: 1px solid #e2e8f0;
            display: flex;
            flex-direction: column;
            box-shadow: -2px 0 8px rgb(0 0 0 / 0.05);
            transition: all 0.2s ease-in-out;
            z-index: 5;
        }
        #sidebar-header {
            padding: 16px 20px;
            background-color: #f8fafc;
            border-bottom: 1px solid #e2e8f0;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        #sidebar-header h3 {
            margin: 0;
            font-size: 16px;
            font-weight: 700;
            color: #0f172a;
        }
        #sidebar-content {
            padding: 20px;
            overflow-y: auto;
            flex-grow: 1;
            font-size: 14px;
            line-height: 1.5;
            color: #334155;
        }
        .json-block {
            background-color: #0f172a;
            color: #38bdf8;
            padding: 12px;
            border-radius: 6px;
            font-family: Consolas, Monaco, monospace;
            font-size: 12px;
            overflow-x: auto;
            margin-top: 10px;
            white-space: pre-wrap;
        }
        #legend {
            display: flex;
            gap: 12px;
            font-size: 12px;
            background-color: #1e293b;
            padding: 6px 12px;
            border-radius: 4px;
            border: 1px solid #475569;
        }
        .legend-item {
            display: flex;
            align-items: center;
            gap: 5px;
        }
        .dot {
            width: 10px;
            height: 10px;
            border-radius: 2px;
        }
        .close-btn {
            background: none;
            border: none;
            color: #94a3b8;
            font-size: 18px;
            cursor: pointer;
        }
        .close-btn:hover {
            color: #0f172a;
        }
        #filter-panel {
            position: absolute;
            top: 20px;
            left: 20px;
            background-color: rgb(255 255 255 / 0.95);
            padding: 15px;
            border-radius: 8px;
            box-shadow: 0 4px 12px rgb(0 0 0 / 0.1);
            border: 1px solid #e2e8f0;
            z-index: 2;
            display: flex;
            flex-direction: column;
            gap: 8px;
            font-size: 13px;
        }
        .filter-checkbox {
            display: flex;
            align-items: center;
            gap: 8px;
            cursor: pointer;
        }
    </style>
</head>
<body>
    <div id="header">
        <h1>🌌 KAOS Đồ thị Nhân-Duyên-Quả (Knowledge Graph)</h1>
        <div id="controls-wrapper">
            <input type="text" id="search-box" placeholder="Tìm tên Task..." onkeydown="if(event.key==='Enter') searchNode()">
            <select id="view-selector" class="control-element" onchange="updateView()">
                <option value="causal">🌟 Góc nhìn: Nhân-Duyên-Quả (Causal Flow)</option>
                <option value="dependency">💻 Góc nhìn: Phụ thuộc Phần mềm (Dependency)</option>
            </select>
            <select id="layout-selector" class="control-element" onchange="updateLayout()">
                <option value="free">🌐 Bố cục: Mạng lưới tự do</option>
                <option value="hierarchical-tb">📐 Bố cục: Phân tầng dọc (Top-Down)</option>
                <option value="hierarchical-lr">📐 Bố cục: Phân tầng ngang (Left-Right)</option>
            </select>
            <div id="legend">
                <div class="legend-item"><span class="dot" style="background-color: #dbeafe; border: 1px solid #2563eb;"></span> <b>Nhân</b></div>
                <div class="legend-item"><span class="dot" style="background-color: #fef9c3; border: 1px solid #ca8a04;"></span> <b>Duyên</b></div>
                <div class="legend-item"><span class="dot" style="background-color: #ffedd5; border: 1px solid #ea580c;"></span> <b>Duyên động</b></div>
                <div class="legend-item"><span class="dot" style="background-color: #d1fae5; border: 1px solid #059669;"></span> <b>Quả OK</b></div>
                <div class="legend-item"><span class="dot" style="background-color: #fee2e2; border: 1px solid #dc2626;"></span> <b>Quả FAIL</b></div>
            </div>
        </div>
    </div>
    
    <div id="main-body">
        <div id="filter-panel">
            <b style="color:#0f172a; margin-bottom:4px; display:block;">Bộ lọc Đồ thị</b>
            <label class="filter-checkbox"><input type="checkbox" id="filter-static-cond" checked onchange="applyFilters()"> Hiện Duyên tĩnh (Spec/Skill)</label>
            <label class="filter-checkbox"><input type="checkbox" id="filter-dynamic-cond" checked onchange="applyFilters()"> Hiện Duyên động (Feedback)</label>
            <label class="filter-checkbox"><input type="checkbox" id="filter-results" checked onchange="applyFilters()"> Hiện Quả (Result Nodes)</label>
            <label class="filter-checkbox"><input type="checkbox" id="filter-only-failed" onchange="applyFilters()"> Chỉ hiện Task Lỗi (FAILED)</label>
        </div>

        <div id="network-container">
            <div id="mynetwork"></div>
        </div>

        <div id="sidebar" style="margin-right: -380px;">
            <div id="sidebar-header">
                <h3 id="sidebar-title">Chi tiết Thực thể</h3>
                <button class="close-btn" onclick="closeSidebar()">&times;</button>
            </div>
            <div id="sidebar-content">
                Chọn một đỉnh (Node) trên đồ thị để xem chi tiết.
            </div>
        </div>
    </div>

    <script type="text/javascript">
        // Data parsed from python
        const rawNodes = __NODES_DATA__;
        const rawEdges = __RAW_EDGES_DATA__;

        const nodes = new vis.DataSet([]);
        const edges = new vis.DataSet([]);
        
        let networkInstance = null;
        let activeViewMode = "causal";
        let activeLayoutMode = "free";

        // Hàm lọc và nạp Nodes dựa trên Checkboxes
        function applyFilters() {
            const showStatic = document.getElementById("filter-static-cond").checked;
            const showDynamic = document.getElementById("filter-dynamic-cond").checked;
            const showResults = document.getElementById("filter-results").checked;
            const onlyFailedTasks = document.getElementById("filter-only-failed").checked;

            const filteredNodes = [];
            const excludedNodeIds = new Set();

            rawNodes.forEach(node => {
                let keep = true;

                if (node.group === "condition") {
                    if (node.cond_type === "feedback" && !showDynamic) keep = false;
                    if (node.cond_type !== "feedback" && !showStatic) keep = false;
                } else if (node.group === "result") {
                    if (!showResults) keep = false;
                } else if (node.group === "task") {
                    if (onlyFailedTasks && node.status !== "FAILED") keep = false;
                }

                if (keep) {
                    filteredNodes.push(node);
                } else {
                    excludedNodeIds.add(node.id);
                }
            });

            // Cập nhật lên vis.js DataSet
            nodes.clear();
            nodes.add(filteredNodes);

            // Cập nhật lại các Cạnh tương ứng (bỏ các cạnh liên quan node bị ẩn)
            updateView(excludedNodeIds);
        }

        // Hàm tính toán và render Edges dựa trên Option góc nhìn
        function updateView(excludedNodeIds = new Set()) {
            const selector = document.getElementById("view-selector");
            activeViewMode = selector.value;
            
            edges.clear();
            const newEdges = [];
            
            rawEdges.forEach(edge => {
                // Bỏ qua nếu có node nằm trong danh sách ẩn
                if (excludedNodeIds.has(edge.task) || excludedNodeIds.has(edge.parent) || 
                    excludedNodeIds.has(edge.condition) || excludedNodeIds.has(edge.result)) {
                    return;
                }

                if (edge.type === "depends_on") {
                    newEdges.push({
                        from: edge.task,
                        to: edge.parent,
                        label: "DEPENDS_ON",
                        color: { color: "#3b82f6", highlight: "#1d4ed8" },
                        arrows: "to",
                        dashes: true
                    });
                } else if (edge.type === "requires") {
                    if (activeViewMode === "causal") {
                        newEdges.push({
                            from: edge.condition,
                            to: edge.task,
                            label: "FEEDS",
                            color: { color: "#f59e0b", highlight: "#d97706" },
                            arrows: "to"
                        });
                    } else {
                        newEdges.push({
                            from: edge.task,
                            to: edge.condition,
                            label: "REQUIRES",
                            color: { color: "#f59e0b", highlight: "#d97706" },
                            arrows: "to"
                        });
                    }
                } else if (edge.type === "produces") {
                    newEdges.push({
                        from: edge.task,
                        to: edge.result,
                        label: "PRODUCES",
                        color: { color: "#10b981", highlight: "#047857" },
                        arrows: "to"
                    });
                } else if (edge.type === "mutates") {
                    if (activeViewMode === "causal") {
                        newEdges.push({
                            from: edge.result,
                            to: edge.condition,
                            label: "MUTATES",
                            color: { color: "#8b5cf6", highlight: "#6d28d9" },
                            arrows: "to",
                            dashes: true
                        });
                    } else {
                        newEdges.push({
                            from: edge.condition,
                            to: edge.result,
                            label: "MUTATED_BY",
                            color: { color: "#8b5cf6", highlight: "#6d28d9" },
                            arrows: "to",
                            dashes: true
                        });
                    }
                }
            });
            
            edges.add(newEdges);
        }

        // Thay đổi Layout (Tự do vs Phân tầng)
        function updateLayout() {
            const selector = document.getElementById("layout-selector");
            activeLayoutMode = selector.value;
            
            let hierarchicalOptions = { enabled: false };
            
            if (activeLayoutMode === "hierarchical-tb") {
                hierarchicalOptions = {
                    enabled: true,
                    direction: "UD",
                    sortMethod: "directed",
                    nodeSpacing: 150,
                    levelSeparation: 150
                };
            } else if (activeLayoutMode === "hierarchical-lr") {
                hierarchicalOptions = {
                    enabled: true,
                    direction: "LR",
                    sortMethod: "directed",
                    nodeSpacing: 150,
                    levelSeparation: 200
                };
            }

            networkInstance.setOptions({
                layout: { hierarchical: hierarchicalOptions },
                physics: { enabled: activeLayoutMode === "free" }
            });
            
            if (activeLayoutMode !== "free") {
                // Cho phép physics chạy một lát rồi ổn định lại vị trí cây
                setTimeout(() => {
                    networkInstance.setOptions({ physics: { enabled: false } });
                }, 1000);
            }
        }

        // Tìm kiếm Task
        function searchNode() {
            const searchVal = document.getElementById("search-box").value.trim().toLowerCase();
            if (!searchVal) return;

            const foundNode = rawNodes.find(n => n.id.toLowerCase().includes(searchVal) || (n.raw_data && n.raw_data.title && n.raw_data.title.toLowerCase().includes(searchVal)));
            if (foundNode) {
                // Kiểm tra xem node có bị lọc mất không
                if (!nodes.get(foundNode.id)) {
                    alert(`Node "${foundNode.id}" hiện đang bị ẩn bởi bộ lọc. Hãy bật lại bộ lọc.`);
                    return;
                }
                networkInstance.selectNodes([foundNode.id]);
                networkInstance.focus(foundNode.id, {
                    scale: 1.2,
                    animation: {
                        duration: 1000,
                        easingFunction: "easeInOutQuad"
                    }
                });
                showNodeDetails(foundNode.id);
            } else {
                alert(`Không tìm thấy Task/Node nào khớp với từ khóa "${searchVal}"`);
            }
        }

        // Hiển thị panel chi tiết bên phải
        function showNodeDetails(nodeId) {
            const node = rawNodes.find(n => n.id === nodeId);
            if (!node) return;

            const sidebar = document.getElementById("sidebar");
            const title = document.getElementById("sidebar-title");
            const content = document.getElementById("sidebar-content");

            // Mở sidebar
            sidebar.style.marginRight = "0px";

            let detailsHtml = "";
            
            if (node.group === "task") {
                title.innerHTML = `📋 Task: ${node.id}`;
                detailsHtml = `
                    <p><b>Tiêu đề:</b> ${node.raw_data.title || "N/A"}</p>
                    <p><b>Module:</b> <span class="dot" style="background-color:#dbeafe; width:auto; height:auto; padding:2px 6px; border-radius:4px; font-weight:bold; color:#1e40af;">${node.raw_data.module || "N/A"}</span></p>
                    <p><b>Độ phức tạp:</b> ${node.raw_data.complexity || "N/A"}</p>
                    <p><b>Trạng thái:</b> <span style="color: ${node.status === 'SUCCESS' ? '#059669' : node.status === 'FAILED' ? '#dc2626' : '#4b5563'}; font-weight:bold;">${node.status}</span></p>
                    <p><b>Mô tả chi tiết:</b></p>
                    <div style="background:#f1f5f9; padding:10px; border-radius:4px; font-style:italic;">${node.raw_data.description || "Không có mô tả"}</div>
                `;
            } else if (node.group === "condition") {
                title.innerHTML = `⚡ Duyên: ${node.id}`;
                detailsHtml = `
                    <p><b>Loại điều kiện:</b> <code>${node.cond_type}</code></p>
                    <p><b>Nội dung / Context:</b></p>
                    <div class="json-block">${node.raw_data.content || "N/A"}</div>
                `;
            } else if (node.group === "result") {
                title.innerHTML = `📊 Quả: Result Attempt ${node.raw_data.attempt}`;
                const success = node.success;
                detailsHtml = `
                    <p><b>Trạng thái thực thi:</b> <b style="color: ${success ? '#059669' : '#dc2626'}">${success ? 'THÀNH CÔNG (SUCCESS)' : 'THẤT BẠI (FAILED)'}</b></p>
                    <p><b>Số lần thử (Attempt):</b> ${node.raw_data.attempt}</p>
                    <p><b>Files tạo mới:</b> <code>${node.raw_data.files_created || '[]'}</code></p>
                    <p><b>Files chỉnh sửa:</b> <code>${node.raw_data.files_modified || '[]'}</code></p>
                    ${!success ? `<p><b>Chi tiết lỗi (Compiler/Linter):</b></p><div class="json-block" style="color:#f43f5e;">${node.raw_data.error_message || "N/A"}</div>` : ''}
                `;
            }

            content.innerHTML = detailsHtml;
        }

        function closeSidebar() {
            document.getElementById("sidebar").style.marginRight = "-380px";
            if (networkInstance) networkInstance.unselectNodes();
        }

        // Khởi tạo đồ thị vis.js
        const container = document.getElementById('mynetwork');
        const data = {
            nodes: nodes,
            edges: edges
        };
        const options = {
            nodes: {
                font: {
                    size: 13,
                    face: 'sans-serif'
                }
            },
            edges: {
                width: 2,
                font: {
                    size: 11,
                    align: 'middle'
                },
                smooth: {
                    type: 'cubicBezier',
                    forceDirection: 'none',
                    roundness: 0.5
                }
            },
            physics: {
                enabled: true,
                solver: 'barnesHut',
                barnesHut: {
                    gravitationalConstant: -2000,
                    centralGravity: 0.3,
                    springLength: 95,
                    springConstant: 0.04,
                    damping: 0.09,
                    avoidOverlap: 1
                },
                stabilization: {
                    enabled: true,
                    iterations: 1000,
                    updateInterval: 100,
                    onlyDynamicEdges: false,
                    fit: true
                }
            }
        };

        networkInstance = new vis.Network(container, data, options);

        // Khởi động đồ thị lần đầu (Lọc và render)
        applyFilters();

        // Lắng nghe sự kiện click chọn Node
        networkInstance.on("click", function (params) {
            if (params.nodes.length > 0) {
                showNodeDetails(params.nodes[0]);
            } else {
                closeSidebar();
            }
        });

        // Dừng physics sau khi đồ thị đã ổn định để tránh chạy liên tục và giật màn hình
        networkInstance.on("stabilizationIterationsDone", function () {
            if (activeLayoutMode === "free") {
                networkInstance.setOptions({ physics: false });
                console.log("Graph stabilized. Physics disabled.");
            }
        });
    </script>
</body>
</html>
"""

    # Thực hiện thay thế các placeholders
    html_template = html_template.replace("__REDIS_PORT__", str(redis_port))
    html_template = html_template.replace("__NODES_DATA__", json.dumps(nodes_list))
    html_template = html_template.replace("__RAW_EDGES_DATA__", json.dumps(raw_edges))

    output_path = Path(__file__).parent.parent / "graph_visualizer.html"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_template)
    
    print(f"🎉 Đã sinh file giao diện đồ thị trực quan mới tại: {output_path.resolve()}")
    print("👉 Hãy mở file này bằng trình duyệt của bạn (hoặc chạy double-click vào file).")
    
    # Tự động mở trình duyệt
    try:
        webbrowser.open(output_path.as_uri())
    except Exception:
        pass

if __name__ == "__main__":
    generate_visualization()
