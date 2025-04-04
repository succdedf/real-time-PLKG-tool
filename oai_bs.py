import socket
import subprocess
import re
import hashlib
import zlib
import numpy as np
import threading
import select
import time
import os
import queue

# 数据预处理（MAF滤波器）
def MAF(windows, step, Data):
    result = []
    k = 0
    while k < Data.size - windows + 1:
        sum = 0
        l = 0
        while l < windows:
            sum += Data[k+l]
            l += 1
        result.append(sum/windows)
        k += step
    return result

# 量化函数
def Quantization_mine(Data):
    i = 1
    result = 0
    while i < len(Data)-1:
        diff_h = Data[i] - Data[i-1]
        diff_t = Data[i+1] - Data[i]

        mono = diff_t * diff_h
        con = diff_t - diff_h

        result *= 2
        if mono >= 0:
            result += 1
        result *= 2
        if con >= 0:
            result += 1
        i += 1

    return result

# 级联协议函数
def Cascade_Cal(K, len):
    result = 0
    one = 1
    i = 0
    while i < len:
        j = 0
        bit = 0
        while j < 3:
            bit = bit ^ (K & 1)
            K = K >> 1
            j += 1
        if bit == 1:
            result |= one
        one = one << 1
        i += 3
    return result

def Cascade_Com(K1, K2, C1, C2, len):
    R1 = 0
    R2 = 0
    new_len = len
    mask = 7

    Diff = C1 ^ C2
    i = 0
    while i < len:
        if Diff & 1 == 0:
            R1 = R1 | (K1 & mask)
            R2 = R2 | (K2 & mask)
            mask = mask << 3
        else:
            new_len -= 3
            K1 = K1 >> 3
            K2 = K2 >> 3
        Diff = Diff >> 1
        i += 3
    return [R1, R2, new_len]

def Cascade_Tran(K, len):
    result = 0
    i = 0
    while i < len:
        if i < len//3:
            index = 3 * i
        elif i < (len//3) * 2:
            index = 3 * (i-len//3) + 1
        else:
            index = 3 * (i-len//3-len//3) + 2
        bit = (K >> index) & 1
        result = result << 2
        result = result | bit
        i += 1
    return result

def Cascade(QD, QU, len_Q):
    len_1 = len_Q - len_Q%3

    CD_1 = Cascade_Cal(QD, len_1)
    CU_1 = Cascade_Cal(QU, len_1)
    [RD_1, RU_1, len_2] = Cascade_Com(QD, QU, CD_1, CU_1, len_1)

    RD_T = Cascade_Tran(RD_1, len_2)
    RU_T = Cascade_Tran(RU_1, len_2)

    CD_2 = Cascade_Cal(RD_T, len_2)
    CU_2 = Cascade_Cal(RU_T, len_2)
    [RD, RU, len_C] = Cascade_Com(RD_T, RU_T, CD_2, CU_2, len_2)

    result = [RD, RU, len_C]
    return result

log_file_path = "/tmp/oai-gnb.log"
# 使用线程安全队列存储从日志中解析出的 (frame, ul_ch_data)
frame_queue = queue.Queue()

def monitor_log():
    """
    模拟 tail -f 监控日志文件，从中提取包含 "frame:" 的行，
    并读取紧跟的下一行作为信道数据，将 (frame, ul_ch_data) 放入队列中
    """
    # 如果日志文件不存在则等待
    while not os.path.exists(log_file_path):
        time.sleep(0.1)
    with open(log_file_path, "r") as f:
        # 将文件指针移到末尾，忽略之前内容
        f.seek(0, 2)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.1)
                continue
            decoded_line = line.strip()
            # 只提取包含 frame 信息的行
            if "frame:" in decoded_line:
                match = re.search(r"frame:(\d+)", decoded_line)
                if match:
                    frame = int(match.group(1))
                    # 尝试读取下一行作为信道数据
                    ul_ch_line = f.readline()
                    if not ul_ch_line:
                        # 没有新数据则等待
                        time.sleep(0.1)
                        continue
                    ul_ch_line_decoded = ul_ch_line.strip()
                    match_ul_ch = re.findall(r"(\d+)", ul_ch_line_decoded)
                    ul_ch_data = list(map(int, match_ul_ch))
                    # 将解析的数据放入队列中
                    frame_queue.put((frame, ul_ch_data))
            # 否则忽略当前行

# 统计变量
key_count = 0
success_count = 0
start_time = None

def key_generation(ul_ch, key_conn, lock):
    global key_count, success_count, start_time
    with lock:
        if key_count % 100 == 0:
            if key_count > 0:
                success_rate = (success_count / 100) * 100
                elapsed_time = time.time() - start_time
                print(f"\n100 keys: Success Rate = {success_rate:.2f}%, Time Taken = {elapsed_time:.2f} seconds")
            success_count = 0
            start_time = time.time()
    
        print("Starting key generation process...")
        windows = 16
        step = 4
        UL = MAF(windows, step, np.array(ul_ch).reshape(-1, 1))
        Quan_len = (len(UL)-2) * 2
        Quan_UL = Quantization_mine(UL)
     
        message = f"{Quan_UL}"
        key_conn.sendall(message.encode())
        print("Sent part key:", message)
     
        data = key_conn.recv(1024).decode()
        print("Received part key from A:", data)
        part_key_a = int(data)
       
        [Cas_UL, Cas_A, Cas_len] = Cascade(Quan_UL, part_key_a, Quan_len)
        Crc_UL = zlib.crc32(Cas_UL.to_bytes(20, byteorder='little'))
        Crc_A = zlib.crc32(Cas_A.to_bytes(20, byteorder='little'))
        if Crc_UL == Crc_A:
            #print('Data Reconciliation Succeed')
            success_count += 1
            UL_SHA = hashlib.sha256(Cas_UL.to_bytes(20, byteorder='little')).hexdigest()
            print("Final key (BS):", UL_SHA)
        #else:
        #    print('Data Reconciliation Failed')
         
        
        
        key_count += 1

frame_port = 8088
key_port = 8089

lock = threading.Lock()

# 用于存储连接对象的全局变量
frame_conn = None
key_conn = None

def accept_frame():
    global frame_conn
    frame_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    frame_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    frame_sock.bind(('0.0.0.0', frame_port))
    frame_sock.listen(1)
    print("Server frame channel waiting for connection on port", frame_port, "...")
    frame_conn, frame_addr = frame_sock.accept()
    print("Frame channel connected by", frame_addr)
    return frame_conn

def accept_key():
    global key_conn
    key_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    key_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    key_sock.bind(('0.0.0.0', key_port))
    key_sock.listen(1)
    print("Server key channel waiting for connection on port", key_port, "...")
    key_conn, key_addr = key_sock.accept()
    print("Key channel connected by", key_addr)
    return key_conn

# 使用线程并发监听两个端口
frame_thread = threading.Thread(target=accept_frame)
key_thread = threading.Thread(target=accept_key)
frame_thread.start()
key_thread.start()
frame_thread.join()
key_thread.join()

# 确保两个连接都建立成功后再继续后续逻辑
if frame_conn is None or key_conn is None:
    print("连接建立失败")
    exit(1)

# 日志文件路径定义在 log_file_path 变量中
log_file_path = "/home/gjy/openairinterface5g/oai-gnb.log"
# 使用线程安全队列存储从日志中解析出的 (frame, ul_ch_data)
frame_queue = queue.Queue()

# 启动 OAI 时，将输出重定向到日志文件
oai_cmd = (
    "sudo /home/gjy/openairinterface5g/cmake_targets/ran_build/build/nr-softmodem "
    "-O /home/gjy/openairinterface5g/targets/PROJECTS/GENERIC-NR-5GC/CONF/gnb.sa.band78.fr1.106PRB.usrpb210.conf "
    "--gNBs.[0].min_rxtxtime 6 --sa -E --continuous-tx > {} 2>/dev/null".format(log_file_path)
)
process = subprocess.Popen(oai_cmd, shell=True)

# 启动日志监控线程
log_thread = threading.Thread(target=monitor_log, daemon=True)
log_thread.start()

base_frame = None
expected_frame = None
last_frame = None

while True:
    # 非阻塞检查 frame 通道是否有数据
    ready = select.select([frame_conn], [], [], 0.2)
    if ready[0]:
        data = frame_conn.recv(1024).decode()
        if not data:
            break
        frame2 = int(data)
    #    print(f"[INFO] Received frame from client: {frame2}")
        last_frame = frame2
    #else:
    #    print("[INFO] No frame received from client.")
   
    # 从日志队列中读取 OAI 的数据
    try:
        frame, ul_ch = frame_queue.get(timeout=0.1)
    except queue.Empty:
        continue

    # 将解析后的 frame 发送给客户端
    frame_conn.sendall(f"{frame}".encode())
    #print(f"[INFO] Sent frame to client: {frame}")

    if base_frame is None:
        base_frame = frame
        expected_frame = base_frame + 10
        if last_frame and last_frame - frame == 1:
            #print("[INFO] Frame match, starting key generation.")
            threading.Thread(target=key_generation, args=(ul_ch, key_conn, lock)).start()
    else:
        if frame == expected_frame:
            frame_conn.sendall(f"{frame}".encode())
            #print(f"[INFO] Sent frame to client: {frame}")
            #print("[INFO] Expected frame reached, triggering key generation.")
            threading.Thread(target=key_generation, args=(ul_ch, key_conn, lock)).start()
            expected_frame += 10

    if expected_frame is not None and expected_frame > 1023:
        base_frame = None
        expected_frame = None
