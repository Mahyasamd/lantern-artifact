import sys
import os
import argparse
import logging
import subprocess
import http.server
import socketserver
import threading
import re
import random
import shutil
import string

output_path = ""
PORT = 8000
HOST = "http://127.0.0.1"

ERROR_TYPES = ["trace", "crash", "sanitizer", "fatal"]
IGNORED_ERRORS = []
SENSITIVE_STRING = []

TEST_CASE_GENERATION = {
   #  "freedom": "python3 ~/fuzz-dom/freedom/main.py -i 1 -m generate -n {num} -o {dest}"
}

CHROMIUM_SYMBOLIZE_BIN = "~/chromium/src/tools/valgrind/asan/asan_symbolize.py"

def generate_random_string(n):
    return ''.join(random.choice(string.ascii_uppercase + string.ascii_lowercase + string.digits) for _ in range(n))

def generate_random_filename(prefix):
    return prefix + generate_random_string(10)

def limit_string_length_to_n(str, n):
    if len(str) > n:
        return str[:n]
    return str

def filter_special_char(str):
    return re.sub(r'[^\w]', '', str)
    
def check_if_path_absolute(path):
    if path[0] == "/":
        return True
    return False

def execute_cmd(cmd):
    logging.info("Executing: " + cmd)
    return subprocess.check_output(cmd, shell=True).decode()
    
def symbolize_stacktrace(source, dest):
    abspath_source = os.path.join(output_path, source)
    abspath_dest = os.path.join(output_path, dest)
    cmd = "cat " + abspath_source + " | " + CHROMIUM_SYMBOLIZE_BIN
    try:
        clean_log = remove_substrings_from_str(execute_cmd(cmd), SENSITIVE_STRING)
        save_str_to_file(clean_log, abspath_dest)
    except:
        logging.info("symbolize failed.")

def pick_random_index_from_dict(dict):
    return random.choice(list(dict.keys()))

def pick_random_from_dict(dict):
    return random.choice(list(dict.values()))
    
def gen_test_case(gen_cmd, number_of_test_cases, output_path):
    if output_path[-1] == "/":
        output_path = output_path[:-1]
    try:
        final_cmd = gen_cmd.format(num=str(number_of_test_cases), dest=output_path)
    except:
        logging.error("Error when formatting cmd.")
        return
    execute_cmd(final_cmd)

def get_current_home_path():
    return os.path.expanduser('~') + "/"

def remove_substrings_from_str(str, substr_list):
    for substr in substr_list:
        str = str.replace(substr, "")
    return str

def save_bug_report_template(error_msg, str):
    if "Check failed:" in str:
        try:
            culprit_source_file = re.search(r'FATAL:(.*?)\]', str).group(1)
            bug_title = "DCHECK failure in " + \
                culprit_source_file + ", Check failed: " + \
                re.search(r'Check failed: (.*?)\s', str).group(1)
        except:
            bug_title = "DCHECK UNKNOWN"
    elif "AddressSanitizer" in str:
        try:
            asan_error_type = re.search(r'AddressSanitizer: (.*?)\s', str).group(1)
        except:
            asan_error_type = "UNKNOWN"
        bug_title = "AddressSanitizer found " + asan_error_type
    elif "FATAL:" in str:
        try:
            culprit_source_file = re.search(r'FATAL:(.*?)\]', str).group(1)
        except:
            culprit_source_file = "UNKNOWN"
        bug_title = "Crash in " + culprit_source_file
    else:
        bug_title = "Unknown error"

    bug_title = remove_substrings_from_str(bug_title, SENSITIVE_STRING)
    
    with open(os.path.join(output_path, error_msg, "summary.txt"), 'w') as f:
        f.write(bug_title)


def extract_error_message(str):
    if "Check failed:" in str:
        try:
            message = "DCHECK_" + re.search(r'Check failed: (.*?)\s', str).group(1)
        except:
            message = "DCHECK_UNKNOWN"
    elif "AddressSanitizer" in str:
        try:
            message = "ASAN_" + re.search(r'AddressSanitizer: (.*?)\s', str).group(1)
        except:
            message = "ASAN_UNKNOWN"
    elif "FATAL:" in str:
        try:
            message = "CRASH_" + re.search(r'FATAL:(.*?)\s', str).group(1)
        except:
            message = "CRASH_UNKNOWN"
    elif "UndefinedBehaviorSanitizer:" in str:
        try:
            message = "UBSAN_" + \
                re.search(r'(?s:.*)UndefinedBehaviorSanitizer: (.*?) (.*?)\s', str).group(1) + "_" + \
                re.search(r'(?s:.*)UndefinedBehaviorSanitizer: (.*?) (.*?)\s', str).group(2)
        except:
            message = "UBSAN_UNKNOWN"
    else:
        message = "UNKNOWN"
    
    return limit_string_length_to_n(filter_special_char(message), 40)

def any_substr_in_str(str, substr_list):
    for substr in substr_list:
        if substr in str:
            return True
    return False


def check_if_poc_folder_exist(str):
    return os.path.exists(os.path.join(output_path, str))

def create_folder(str):
    if not os.path.exists(os.path.join(output_path, str)):
        os.makedirs(os.path.join(output_path, str))

def create_poc_folder(str):
    if not os.path.exists(os.path.join(output_path, str)):
        os.makedirs(os.path.join(output_path, str))

def save_str_to_file(str, filename):
    with open(os.path.join(output_path, filename), 'w') as f:
        f.write(str)

def copy_file(orig_file, dest_file):
    with open(orig_file, 'r') as f:
        with open(dest_file, 'w') as f2:
            f2.write(f.read())

def remove_folder_if_exist(path):
    if os.path.exists(path):
        shutil.rmtree(path)

def remove_all_files_from_folder(path):
    for f in os.listdir(path):
        try:
            os.remove(os.path.join(path, f))
        except:
            logging.error("Error when removing file: " + f)


def launch_test_case(browser_cmd, test_case, max_wait=15, js_path=None, hardcoded_line=None):
    cmd = '{} {}'.format(browser_cmd, HOST + test_case).split()
    logging.info("Opening test case {}".format(test_case))
    fuzzing_env = os.environ.copy()
    fuzzing_env["ASAN_OPTIONS"] = "detect_odr_violation=0"
    proc = subprocess.Popen(
        cmd, 
        stdout=subprocess.PIPE, 
        stderr=subprocess.PIPE, 
        env=fuzzing_env)
    try:
        outs, errs = proc.communicate(timeout=max_wait)
        logging.info('Test case {} exited'.format(test_case))
    except subprocess.TimeoutExpired:
        proc.kill()
        outs, errs = proc.communicate()
        logging.info('Test case {} terminated'.format(test_case))
    outs = outs.decode()
    errs = errs.decode()
    logging.info(outs)
    logging.warning(errs)

    if any_substr_in_str(errs.lower(), ERROR_TYPES) and \
        not any_substr_in_str(errs.lower(), IGNORED_ERRORS):
        logging.warning("Crashed!")
        error_msg = extract_error_message(errs)

        if not check_if_poc_folder_exist(error_msg):
            create_poc_folder(error_msg)
            save_bug_report_template(error_msg, errs)

        test_case_folder = os.path.join(error_msg, test_case)
        create_poc_folder(test_case_folder)
        clean_log = remove_substrings_from_str(errs, SENSITIVE_STRING)

        save_str_to_file(clean_log, 
            os.path.join(test_case_folder, "trace.txt"))
        
        save_str_to_file(errs, 
            os.path.join(test_case_folder, "trace.tmp"))
        symbolize_stacktrace(
            os.path.join(test_case_folder, "trace.tmp"), 
            os.path.join(test_case_folder, "trace_sym.txt"))
        os.remove(os.path.join(output_path, test_case_folder, "trace.tmp"))

        copy_file(
            os.path.join(input_path, test_case), 
            os.path.join(output_path, test_case_folder, "poc.html"))
            
        crash_info = []
        if js_path:
            crash_info.append(f"Crash folder: {os.path.dirname(js_path)}")
            crash_info.append(f"standalone7.js: {js_path}")
        if hardcoded_line:
            crash_info.append(f"Crashing query line: {hardcoded_line.strip()}")

        if js_path and os.path.exists(js_path):
            with open(js_path, "r") as f:
                for l in f:
                    if ".spec.js" in l:
                        crash_info.append(f"Referenced .spec.js: {l.strip()}")
                        break

        if crash_info:
            with open(os.path.join(output_path, test_case_folder, "hardcodedQuery_info.txt"), "a") as f:
                f.write("\n".join(crash_info) + "\n---\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='HTML executer')
    parser.add_argument("-i", dest="input", help="test case input folder", required=False)
    parser.add_argument("-o", dest="output", help="output folder")
    parser.add_argument("-b", dest="browser", help="browser command line")
    parser.add_argument("-p", dest="port", help="port to run the server on", required=False)

    args = parser.parse_args()
    
    if check_if_path_absolute(args.output):
        output_path = args.output
    else:
        output_path = os.path.join(os.getcwd(), args.output)

    continuous_fuzzing = False

    input_folders = []
    if args.input:
        if "*" in args.input:
            import glob
            input_folders = sorted(glob.glob(args.input))
        else:
            input_folders = [args.input]
    else:
        continuous_fuzzing = True
        tmp_folder = os.path.join(os.getcwd(), generate_random_filename(".tmp"))
        remove_folder_if_exist(tmp_folder)
        create_folder(tmp_folder)
        input_folders = [tmp_folder]

    user_profile_path = os.path.join(os.getcwd(), generate_random_filename(".tmp_usr"))
    browser_cmd = args.browser + " --user-data-dir=" + user_profile_path

    if not os.path.exists(output_path):
        os.mkdir(output_path)

    SENSITIVE_STRING.append(get_current_home_path())

    logging.basicConfig(level=logging.INFO,
	    format='%(asctime)s %(levelname)-12s %(message)s', 
	    datefmt='%m/%d/%Y %I:%M:%S %p', 
	    filename=os.path.join(output_path, 'fuzz.log'), filemode='a+')

    if args.port:
        PORT = int(args.port)
    HOST = HOST + ":" + str(PORT) + "/"

    logging.info("Start fuzzing")

    # Loop through all cts_mutated* folders
    for input_path in input_folders:
        if not os.path.exists(input_path):
            print(f"Input folder does not exist: {input_path}")
            continue

        logging.info(f"Processing input folder: {input_path}")

        # --- FIX START ---
        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=input_path, **kwargs)
        class ReusableTCPServer(socketserver.TCPServer):
            allow_reuse_address = True

        server = ReusableTCPServer(("", PORT), Handler)
       # server = socketserver.TCPServer(("", PORT), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        logging.info(f"Serving {input_path} at port {PORT}")
        # --- FIX END ---

        if continuous_fuzzing:
            logging.info("Skipping test case generation (Freedom removed).")

        filename = os.path.join(input_path, "out", "common", "runtime", "standalone7.js")

        if not os.path.exists(filename):
            logging.warning(f"standalone7.js not found in {input_path}")
            server.shutdown()
            server.server_close()
            continue

        with open(filename, "r") as f:
            lines = f.readlines()

        for idx, line in enumerate(lines):
            if line.strip().startswith("// const hardcodedQuery"):
                for j, l in enumerate(lines):
                    if l.strip().startswith("const hardcodedQuery"):
                        lines[j] = "// " + l
                uncommented_line = line.replace("// ", "", 1)
                lines[idx] = uncommented_line
                with open(filename, "w") as f:
                    f.writelines(lines)
                logging.info(f"Launching Chromium with file: {os.path.join(input_path, 'standalone/index7.html')}")
                launch_test_case(
                    browser_cmd,
                    "standalone/index7.html",
                    15,
                    js_path=filename,
                    hardcoded_line=uncommented_line
                )
                lines[idx] = line
                with open(filename, "w") as f:
                    f.writelines(lines)

        # --- FIX START ---
        logging.info(f"Finished processing {input_path}")
        server.shutdown()
        server.server_close()
        # --- FIX END ---

    logging.info("Finished processing all input folders.")

