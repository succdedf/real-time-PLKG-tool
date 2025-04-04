import socket
import subprocess
import re
import hashlib
import zlib
import numpy as np
import threading
import os
import queue
import time

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


log_file_path = "/home/ue_test/openairinterface5g/oai-ue.log"
# 使用线程安全队列存储从日志中解析出的 (frame, dl_ch_data)
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
                    dl_ch_line = f.readline()
                    if not dl_ch_line:
                        # 没有新数据则等待
                        time.sleep(0.1)
                        continue
                    dl_ch_line_decoded = dl_ch_line.strip()
                    match_dl_ch = re.findall(r"(\d+)", dl_ch_line_decoded)
                    dl_ch_data = list(map(int, match_dl_ch))
                    # 将解析的数据放入队列中
                    frame_queue.put((frame, dl_ch_data))
            # 否则忽略当前行


                
def key_generation(dl_ch, key_sock, lock):
    with lock:
        print("Starting key generation process...")
        windows = 16
        step = 4
        DL = MAF(windows, step, np.array(dl_ch).reshape(-1, 1))
        Quan_len = (len(DL)-2) * 2
        Quan_DL = Quantization_mine(DL)
     
        # 通过专用的 key 通道发送部分密钥到电脑B
        message = f"{Quan_DL}"
        key_sock.sendall(message.encode())
        print("Sent part key:", message)
     
        # 接收电脑B的部分密钥
        data = key_sock.recv(1024).decode()
        print("Received part key from B:", data)
        part_key_b = int(data)
     
        [Cas_DL, Cas_B, Cas_len] = Cascade(Quan_DL, part_key_b, Quan_len)
        Crc_DL = zlib.crc32(Cas_DL.to_bytes(20, byteorder='little'))
        Crc_B = zlib.crc32(Cas_B.to_bytes(20, byteorder='little'))
        if Crc_DL == Crc_B:
            #print('Data Reconciliation Succeed')
            DL_SHA = hashlib.sha256(Cas_DL.to_bytes(20, byteorder='little')).hexdigest()
            print("Final key (UE):", DL_SHA)
        #else:
            #print('Data Reconciliation Failed')
         
        

# 客户端连接参数（根据实际情况修改IP和端口）
server_ip = '10.195.198.65'
frame_port = 8088
key_port = 8089

lock = threading.Lock()

# 建立两个独立的连接：frame通道与key通道
frame_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
key_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
frame_sock.connect((server_ip, frame_port))
key_sock.connect((server_ip, key_port))
print("Connected to server on both channels.")

# 日志文件路径定义在 log_file_path 变量中
# 启动 OAI 时，将输出重定向到日志文件
oai_cmd = (
    "sudo /home/ue_test/openairinterface5g/cmake_targets/ran_build/build/nr-uesoftmodem "
    "-O /home/ue_test/openairinterface5g/targets/PROJECTS/GENERIC-NR-5GC/CONF/ue.conf "
    "-r 106 --numerology 1 --band 78 -C 3619200000 --ue-fo-compensation --sa -E --uicc0.imsi 001010000000001 > {} 2>/dev/null".format(log_file_path)
)
#print("[INFO] Starting OAI with command:")
#print(oai_cmd)
process = subprocess.Popen(oai_cmd, shell=True)

# 启动日志监控线程
log_thread = threading.Thread(target=monitor_log, daemon=True)
log_thread.start()

send_next_frame = True
last_frame = None

while True:
    # 每次允许发送时读取新帧信息
    if send_next_frame:
        frame, dl_ch = frame_queue.get()
        frame_sock.sendall(f"{frame}".encode())
        #print("Sent frame:", frame)
        last_frame = frame

    # 接收服务端返回的帧信息
    data = frame_sock.recv(1024).decode()
    frame2 = int(data)
    #print("Received frame:", frame2)

    # 判断是否满足条件（例如：frame - frame2 == 1）
    if last_frame is not None and last_frame - frame2 == 1:
        #print("Frame match, starting key generation.")
        send_next_frame = True
        threading.Thread(target=key_generation, args=(dl_ch, key_sock, lock)).start()
    else:
        #print("Frame mismatch.")
        # 根据实际逻辑决定何时允许读取下一帧
        if frame2 >= last_frame:
            send_next_frame = True
        else:
            send_next_frame = False
 

