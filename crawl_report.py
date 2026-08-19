import json
import os
import re
import logging
import time
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Cau hinh logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('cafef_downloader.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Cau hinh
CONFIG = {
    'max_retries': 3,
    'timeout': 30,
    'max_workers': 5,
    'download_folder': 'downloads',
    'min_file_size': 1024,
    'supported_formats': ['application/pdf', 'application/octet-stream']
}

class PDFDownloader:
    """Class quan ly tai PDF tu Cafef"""
    
    def __init__(self):
        self.session = self._create_session()
        self.download_stats = {
            'total': 0,
            'success': 0,
            'failed': 0,
            'skipped': 0
        }
        
    def _create_session(self) -> requests.Session:
        """Tao session voi retry mechanism"""
        session = requests.Session()
        
        retry = Retry(
            total=CONFIG['max_retries'],
            read=CONFIG['max_retries'],
            connect=CONFIG['max_retries'],
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],
        )
        
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7',
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': 'https://cafef.vn/',
            'Connection': 'keep-alive',
        })
        
        return session
    
    def get_financial_reports(self, stock_code: str, exchange: str) -> Tuple[bool, List[Dict], str]:
        """Lay danh sach bao cao tai chinh tu API Cafef"""
        try:
            exchange_map = {
                'HOSE': 1,
                'HNX': 2,
                'UPCOM': 3
            }
            exchange_code = exchange_map.get(exchange.upper(), 1)
            
            api_url = f"https://cafef.vn/du-lieu/Ajax/PageNew/FileBCTC.ashx?Symbol={stock_code.lower()}&Type={exchange_code}&Year=0"
            
            logger.info(f"Dang goi API: {api_url}")
            response = self.session.get(api_url, timeout=CONFIG['timeout'])
            response.raise_for_status()
            
            data = response.json()
            
            if not data.get('Success', False):
                return False, [], f"API tra ve loi: {data.get('Message', 'Khong ro ly do')}"
            
            reports = data.get('Data', [])
            if not isinstance(reports, list):
                return False, [], "Du lieu khong dung dinh dang"
            
            valid_reports = []
            for report in reports:
                if not report.get('Link'):
                    continue
                    
                if not self._is_valid_url(report['Link']):
                    logger.warning(f"Link khong hop le: {report.get('Link')}")
                    continue
                    
                valid_reports.append({
                    'name': report.get('Name', 'Khong co ten'),
                    'link': report['Link'],
                    'year': self._extract_year(report.get('Name', '')),
                    'type': self._classify_report(report.get('Name', ''))
                })
            
            return True, valid_reports, f"Tim thay {len(valid_reports)} bao cao hop le"
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Loi ket noi API: {e}")
            return False, [], f"Loi ket noi: {str(e)}"
        except json.JSONDecodeError as e:
            logger.error(f"Loi parse JSON: {e}")
            return False, [], "Du lieu tra ve khong phai JSON hop le"
        except Exception as e:
            logger.error(f"Loi khong xac dinh: {e}")
            return False, [], f"Loi: {str(e)}"
    
    def _is_valid_url(self, url: str) -> bool:
        """Kiem tra URL hop le"""
        try:
            result = urlparse(url)
            return all([result.scheme, result.netloc])
        except:
            return False
    
    def _extract_year(self, name: str) -> Optional[int]:
        """Trich xuat nam tu ten bao cao"""
        match = re.search(r'(20\d{2})', name)
        return int(match.group(1)) if match else None
    
    def _classify_report(self, name: str) -> str:
        """Phan loai bao cao"""
        name_lower = name.lower()
        if 'cong ty me' in name_lower or 'rieng' in name_lower:
            return 'parent_company'
        elif 'hop nhat' in name_lower or 'consolidated' in name_lower:
            return 'consolidated'
        elif 'quy' in name_lower:
            return 'quarterly'
        elif 'nam' in name_lower:
            return 'annual'
        return 'other'
    
    def download_pdf(self, pdf_link: str, filename: str, folder: str, 
                    check_existing: bool = True) -> Tuple[bool, str]:
        """Tai mot file PDF"""
        try:
            os.makedirs(folder, exist_ok=True)
            filepath = os.path.join(folder, filename)
            
            if check_existing and os.path.exists(filepath):
                file_size = os.path.getsize(filepath)
                if file_size > CONFIG['min_file_size']:
                    logger.info(f"File da ton tai: {filename} ({file_size} bytes)")
                    return True, f"Da ton tai: {filepath}"
            
            logger.info(f"Dang tai: {filename}")
            response = self.session.get(pdf_link, timeout=CONFIG['timeout'], stream=True)
            response.raise_for_status()
            
            content_type = response.headers.get('content-type', '').lower()
            if not any(fmt in content_type for fmt in CONFIG['supported_formats']):
                logger.warning(f"Content-Type khong phai PDF: {content_type}")
            
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            
            with open(filepath, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
            
            if downloaded < CONFIG['min_file_size']:
                os.remove(filepath)
                return False, f"File qua nho ({downloaded} bytes), co the bi loi"
            
            logger.info(f"Da tai thanh cong: {filename} ({downloaded} bytes)")
            return True, f"Da luu: {filepath} ({downloaded//1024} KB)"
            
        except requests.exceptions.Timeout:
            return False, f"Timeout khi tai: {filename}"
        except requests.exceptions.RequestException as e:
            return False, f"Loi tai: {str(e)}"
        except Exception as e:
            logger.error(f"Loi khong xac dinh khi tai {filename}: {e}")
            return False, f"Loi: {str(e)}"
    
    def download_multiple_pdfs(self, pdf_list: List[Dict], stock_code: str, 
                              indices: List[int]) -> Tuple[int, int]:
        """Tai nhieu file PDF song song"""
        if not indices:
            return 0, 0
        
        folder = os.path.join(CONFIG['download_folder'], stock_code)
        os.makedirs(folder, exist_ok=True)
        
        total = len(indices)
        success_count = 0
        
        tasks = []
        for idx in indices:
            if 1 <= idx <= len(pdf_list):
                pdf = pdf_list[idx - 1]
                safe_name = self._sanitize_filename(pdf['name'])
                filename = f"{stock_code}_{idx:02d}_{safe_name}.pdf"
                tasks.append((idx, pdf['link'], filename, folder))
        
        if not tasks:
            return 0, 0
        
        print(f"\nDang tai {len(tasks)} file PDF (su dung {CONFIG['max_workers']} luong)...")
        print("-" * 60)
        
        with ThreadPoolExecutor(max_workers=CONFIG['max_workers']) as executor:
            future_to_task = {
                executor.submit(self.download_pdf, link, filename, folder): (idx, filename)
                for idx, link, filename, folder in tasks
            }
            
            for future in as_completed(future_to_task):
                idx, filename = future_to_task[future]
                try:
                    success, message = future.result()
                    if success:
                        success_count += 1
                        self.download_stats['success'] += 1
                    else:
                        self.download_stats['failed'] += 1
                    
                    print(f"   [{idx}] {message}")
                    
                except Exception as e:
                    logger.error(f"Loi khi tai file #{idx}: {e}")
                    print(f"   [{idx}] Loi: {str(e)}")
                    self.download_stats['failed'] += 1
        
        self.download_stats['total'] += len(tasks)
        return success_count, len(tasks)
    
    def _sanitize_filename(self, name: str) -> str:
        """Tao ten file an toan"""
        safe = re.sub(r'[^\w\s\-]', '', name)
        safe = re.sub(r'\s+', '_', safe.strip())
        return safe[:80] if safe else 'untitled'
    
    def display_report_summary(self, reports: List[Dict]):
        """Hien thi tom tat danh sach bao cao"""
        if not reports:
            print("\nKhong co bao cao nao.")
            return
        
        print("\n" + "=" * 80)
        print("                 DANH SACH BAO CAO TAI CHINH")
        print("=" * 80)
        
        categories = {
            'parent_company': [],
            'consolidated': [],
            'quarterly': [],
            'annual': [],
            'other': []
        }
        
        for i, report in enumerate(reports, 1):
            categories[report['type']].append((i, report))
            
            year_str = f" ({report['year']})" if report['year'] else ""
            print(f"\nFile {i:2d}: {report['name']}{year_str}")
            print(f"    Link: {report['link'][:80]}...")
            
            if i % 5 == 0 and i < len(reports):
                print("-" * 60)
        
        print("\n" + "=" * 80)
        print("                             THONG KE")
        print("=" * 80)
        print(f"\nTong so bao cao: {len(reports)}")
        print(f"   - Bao cao cong ty me: {len(categories['parent_company'])}")
        print(f"   - Bao cao hop nhat: {len(categories['consolidated'])}")
        print(f"   - Bao cao quy: {len(categories['quarterly'])}")
        print(f"   - Bao cao nam: {len(categories['annual'])}")
        print(f"   - Khac: {len(categories['other'])}")

class CafefDownloaderCLI:
    """CLI Interface cho ung dung"""
    
    def __init__(self):
        self.downloader = PDFDownloader()
        self.history = self._load_history()
        
    def _load_history(self) -> List[Dict]:
        """Tai lich su tra cuu"""
        history_file = 'search_history.json'
        if os.path.exists(history_file):
            try:
                with open(history_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return []
        return []
    
    def _save_history(self, stock_code: str, exchange: str, report_count: int):
        """Luu lich su tra cuu"""
        self.history.append({
            'stock_code': stock_code,
            'exchange': exchange,
            'report_count': report_count,
            'timestamp': datetime.now().isoformat()
        })
        
        if len(self.history) > 100:
            self.history = self.history[-100:]
        
        try:
            with open('search_history.json', 'w', encoding='utf-8') as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2)
        except:
            pass
    
    def _get_user_input(self, prompt: str, default: str = "", 
                        valid_options: List[str] = None) -> str:
        """Lay input tu user voi validation"""
        while True:
            if default:
                user_input = input(f"{prompt} [{default}]: ").strip()
                if not user_input:
                    user_input = default
            else:
                user_input = input(f"{prompt}: ").strip()
            
            if not user_input and not default:
                print("Vui long nhap gia tri.")
                continue
                
            if valid_options and user_input not in valid_options:
                print(f"Vui long chon tu: {', '.join(valid_options)}")
                continue
                
            return user_input
    
    def _parse_indices(self, input_str: str, max_index: int) -> List[int]:
        """Parse input indices tu user"""
        if not input_str:
            return []
        
        input_str = input_str.strip().lower()
        indices = []
        
        if input_str == 'all':
            return list(range(1, max_index + 1))
        
        parts = input_str.split(',')
        for part in parts:
            part = part.strip()
            if '-' in part:
                try:
                    start, end = part.split('-')
                    start = int(start.strip())
                    end = int(end.strip())
                    indices.extend(range(start, end + 1))
                except:
                    continue
            else:
                try:
                    idx = int(part)
                    if 1 <= idx <= max_index:
                        indices.append(idx)
                except:
                    continue
        
        return sorted(set(indices))
    
    def run(self):
        """Chay CLI"""
        while True:
            print("\n" + "-" * 80)
            
            stock_code = input("\nNhap ma co phieu (VD: VCB, TCB, FPT) [hoac 'q' de thoat]: ").strip().upper()
            
            if stock_code.lower() in ['q', 'quit', 'exit']:
                self._show_summary()
                print("\nCam on ban da su dung chuong trinh. Hen gap lai!")
                break
            
            if not stock_code:
                print("Vui long nhap ma co phieu.")
                continue
            
            print("\nChon san giao dich:")
            print("   1. HOSE")
            print("   2. HNX")
            print("   3. UPCOM")
            exchange_choice = self._get_user_input("Nhap lua chon", "1", ['1', '2', '3'])
            
            exchange_map = {'1': 'HOSE', '2': 'HNX', '3': 'UPCOM'}
            exchange = exchange_map[exchange_choice]
            
            print("\n" + "=" * 80)
            print(f"DANG TRA CUU: Ma {stock_code} - San {exchange}")
            print("=" * 80)
            
            start_time = time.time()
            success, reports, message = self.downloader.get_financial_reports(stock_code, exchange)
            elapsed = time.time() - start_time
            
            if not success:
                print(f"\n{message}")
                continue
            
            print(f"\n{message} (Thoi gian: {elapsed:.2f}s)")
            
            if not reports:
                print("\nKhong co bao cao nao cho ma nay.")
                continue
            
            self.downloader.display_report_summary(reports)
            self._save_history(stock_code, exchange, len(reports))
            self._handle_download_menu(reports, stock_code)
    
    def _handle_download_menu(self, reports: List[Dict], stock_code: str):
        """Xu ly menu tai file"""
        print("\n" + "=" * 80)
        print("                             MENU TAI FILE")
        print("=" * 80)
        print("   1. Tai mot file PDF")
        print("   2. Tai nhieu file PDF")
        print("   3. Tai tat ca file PDF")
        print("   4. Bo qua")
        print("   5. Tai file theo loai (cong ty me/hop nhat)")
        
        choice = self._get_user_input("\nNhap lua chon", "4", ['1', '2', '3', '4', '5'])
        
        if choice == '1':
            self._download_single_file(reports, stock_code)
        elif choice == '2':
            self._download_multiple_files(reports, stock_code)
        elif choice == '3':
            self._download_all_files(reports, stock_code)
        elif choice == '5':
            self._download_by_type(reports, stock_code)
        else:
            print("Bo qua tai file.")
    
    def _download_single_file(self, reports: List[Dict], stock_code: str):
        """Tai mot file"""
        try:
            idx_input = self._get_user_input(f"Nhap so thu tu file (1-{len(reports)})")
            if idx_input.isdigit():
                idx = int(idx_input)
                if 1 <= idx <= len(reports):
                    self.downloader.download_multiple_pdfs(
                        reports, stock_code, [idx]
                    )
                else:
                    print(f"So thu tu khong hop le. Vui long chon tu 1 den {len(reports)}")
        except Exception as e:
            print(f"Loi: {e}")
    
    def _download_multiple_files(self, reports: List[Dict], stock_code: str):
        """Tai nhieu file"""
        print(f"\nHuong dan: Nhap so thu tu cach nhau bang dau phay (VD: 1,3,5)")
        print(f"   Hoac nhap khoang (VD: 1-5)")
        print(f"   Hoac nhap 'all' de tai tat ca")
        
        idx_input = self._get_user_input(f"Nhap cac so thu tu (1-{len(reports)})")
        indices = self._parse_indices(idx_input, len(reports))
        
        if indices:
            self.downloader.download_multiple_pdfs(reports, stock_code, indices)
        else:
            print("Khong co so thu tu hop le.")
    
    def _download_all_files(self, reports: List[Dict], stock_code: str):
        """Tai tat ca file"""
        confirm = self._get_user_input(
            f"\nBan co chac muon tai tat ca {len(reports)} file PDF? (yes/no)",
            "no",
            ['yes', 'no']
        )
        
        if confirm == 'yes':
            self.downloader.download_multiple_pdfs(
                reports, stock_code, list(range(1, len(reports) + 1))
            )
            print(f"\nFile duoc luu tai: {CONFIG['download_folder']}/{stock_code}/")
        else:
            print("Da huy tai file.")
    
    def _download_by_type(self, reports: List[Dict], stock_code: str):
        """Tai file theo loai"""
        print("\nChon loai bao cao:")
        print("   1. Bao cao cong ty me")
        print("   2. Bao cao hop nhat")
        print("   3. Bao cao quy")
        print("   4. Bao cao nam")
        
        type_choice = self._get_user_input("Nhap lua chon", "1", ['1', '2', '3', '4'])
        type_map = {
            '1': 'parent_company',
            '2': 'consolidated',
            '3': 'quarterly',
            '4': 'annual'
        }
        report_type = type_map[type_choice]
        
        filtered = [(i, r) for i, r in enumerate(reports, 1) if r['type'] == report_type]
        
        if not filtered:
            print(f"Khong tim thay bao cao loai nay.")
            return
        
        print(f"\nTim thay {len(filtered)} bao cao loai nay:")
        for idx, report in filtered:
            print(f"   [{idx}] {report['name']}")
        
        confirm = self._get_user_input(
            f"\nBan co muon tai {len(filtered)} file nay? (yes/no)",
            "no",
            ['yes', 'no']
        )
        
        if confirm == 'yes':
            indices = [idx for idx, _ in filtered]
            self.downloader.download_multiple_pdfs(reports, stock_code, indices)
        else:
            print("Da huy tai file.")
    
    def _show_summary(self):
        """Hien thi tom tat phien lam viec"""
        print("\n" + "=" * 80)
        print("                         TOM TAT PHIEN LAM VIEC")
        print("=" * 80)
        print(f"Tong so file da tai: {self.downloader.download_stats['total']}")
        print(f"   Thanh cong: {self.downloader.download_stats['success']}")
        print(f"   That bai: {self.downloader.download_stats['failed']}")
        print(f"   Bo qua: {self.downloader.download_stats['skipped']}")
        
        if self.history:
            print(f"\nLich su tra cuu ({len(self.history)} records):")
            for record in self.history[-5:]:
                print(f"   - {record['stock_code']} - {record['exchange']}: {record['report_count']} bao cao")
        print("=" * 80)

def main():
    """Entry point"""
    try:
        try:
            import requests
        except ImportError:
            print("Thu vien 'requests' chua duoc cai dat.")
            print("Dang cai dat...")
            os.system('pip install requests urllib3')
            import requests
        
        cli = CafefDownloaderCLI()
        cli.run()
        
    except KeyboardInterrupt:
        print("\n\nChuong trinh bi dung boi nguoi dung.")
    except Exception as e:
        logger.error(f"Loi chuong trinh: {e}", exc_info=True)
        print(f"\nDa xay ra loi: {e}")
        print("Chi tiet da duoc ghi vao file log: cafef_downloader.log")

if __name__ == "__main__":
    main()
