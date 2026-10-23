#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "pyserial==3.5",
# ]
# ///
# decompyle3 version 3.9.0
# Python bytecode version base 3.7.0 (3394)
# Decompiled from: Python 3.7.16 (default, Mar 30 2023, 01:25:49) 
# [GCC 12.2.1 20220924]
# Embedded file name: BLFlashCommand.py
__doc__ = '\nCreated on 20221216\n\n@author: Dillon\n'
import os, re, sys, argparse, binascii, threading, configparser
from libs import bflb_utils
from libs.bflb_utils import get_serial_ports
from libs.bflb_interface_eflash_loader import InterfaceEflashLoader
from chip_profiles import resolve_chip

def hex_to_dec(value):
    try:
        value = value.replace('0x', '')
        int(value, 16)
        return True
    except ValueError:
        return False


class ThreadIot(threading.Thread):

    def __init__(self, config, act='download', callback=None):
        threading.Thread.__init__(self)
        self.obj = InterfaceEflashLoader()
        self.config = config
        self.callback = callback
        self.act = act

    def stop(self):
        pass

    def run(self):
        self.obj.run(self.act, self.config, self.callback)


class MainClass:

    def get_addr_from_partition_by_name(self, name, parition_file, index):
        try:
            with open(parition_file, 'rb') as fp:
                data = bytearray(fp.read())
                fp.close()
                start = data.find(name.encode('utf-8'))
                if start != -1:
                    addr = data[start + 9 + index * 4:start + 9 + 4 + index * 4]
                    addr.reverse()
                    addr = hex(int(binascii.hexlify(addr), 16))
                    return (
                     True, addr)
                print(data)
                print(name.encode('utf-8'))
                return (False, '0')
        except Exception as e:
            try:
                bflb_utils.printf(e)
                return (False, '0')
            finally:
                e = None
                del e

    def get_value(self, args):
        self.config = dict()
        self.config['param'] = dict()
        self.config['check_box'] = dict()
        self.config['input_path'] = dict()
        self.config['param']['interface_type'] = args.interface
        self.config['param']['comport_uart'] = args.port
        try:
            profile = resolve_chip(args.chipname)
        except ValueError as error:
            bflb_utils.printf('Error: ' + str(error))
            return False
        self.config['param']['chip'] = profile.requested_name
        self.config['param']['chip_name'] = profile.resource_name
        self.config['param']['chip_type'] = profile.backend
        if args.interface.lower() == 'uart':
            self.config['param']['speed_uart'] = str(args.baudrate)
            self.config['param']['speed_jlink'] = '1000'
        else:
            self.config['param']['speed_uart'] = '2000000'
            if str(args.baudrate) == '2000000':
                self.config['param']['speed_jlink'] = '1000'
            else:
                self.config['param']['speed_jlink'] = str(args.baudrate)
        if args.efuse:
            self.config['check_box']['efuse'] = True
            self.config['input_path']['efuse'] = os.path.abspath(args.efuse)
        else:
            self.config['check_box']['efuse'] = False
            self.config['input_path']['efuse'] = ''
        if not args.config:
            bflb_utils.printf('Error: --config is required. Pass a flash_prog_cfg.ini with filedir/address entries.')
            return
        config_path = os.path.abspath(args.config)
        if not os.path.isfile(config_path):
            bflb_utils.printf('Error: Config File Not Found: ' + config_path)
            return
        self.config['input_path']['config'] = config_path
        try:
            try:
                self.erase = 1
                self.skip_mode = '0x0, 0x0'
                self.boot2_isp_mode = 0
                config = configparser.ConfigParser()
                config.read(config_path, encoding='utf-8')
                if not any(item != 'cfg' for item in config.sections()):
                    bflb_utils.printf('Error: Config must contain at least one image section with filedir/address entries.')
                    return False
                if config:
                    for item in config.sections():
                        if item == 'cfg':
                            self.erase = config.get('cfg', 'erase', fallback=1)
                            self.skip_mode = config.get('cfg', 'skip_mode', fallback='0x0, 0x0')
                            self.boot2_isp_mode = config.get('cfg', 'boot2_isp_mode', fallback=0)
                        else:
                            filedir = config.get(item, 'filedir')
                            address = config.get(item, 'address')
                            if not filedir.strip() or not address.strip():
                                bflb_utils.printf('Error: ' + item + ' requires nonempty filedir/address entries.')
                                return False
                            if not self.get_value_file(item, filedir, address, args.cpu_id):
                                return False

            except Exception as e:
                try:
                    config = None
                    print('ConfigParser Error: ' + str(e))
                    return False
                finally:
                    e = None
                    del e

        finally:
            self.config['param']['erase'] = self.erase
            self.config['param']['skip_mode'] = self.skip_mode
            self.config['param']['boot2_isp_mode'] = self.boot2_isp_mode

        if args.key:
            self.config['check_box']['encrypt'] = True
            self.config['param']['aes_key'] = args.key
            self.config['param']['aes_iv'] = args.iv
        else:
            self.config['check_box']['encrypt'] = False
            self.config['param']['aes_key'] = ''
            self.config['param']['aes_iv'] = ''
        if args.pk:
            self.config['check_box']['sign'] = True
            self.config['input_path']['publickey'] = args.pk
            self.config['input_path']['privatekey'] = args.sk
        else:
            self.config['check_box']['sign'] = False
            self.config['input_path']['publickey'] = ''
            self.config['input_path']['privatekey'] = ''
        return self.config

    def get_value_file(self, name, path, addr, cpu_id=None):
        name = str(name)
        if os.path.isabs(path):
            path = os.path.abspath(path)
        else:
            config_dir = os.path.dirname(os.path.abspath(self.config['input_path']['config']))
            path = os.path.join(config_dir, path)
        if cpu_id:
            path = path.replace('$(CHIPNAME)', self.config['param']['chip'] + '_' + cpu_id)
        else:
            path = path.replace('$(CHIPNAME)', self.config['param']['chip'])
        addr = str(addr)
        self.config[name] = {}
        self.config[name]['addr'] = addr
        self.config[name]['path'] = path
        if not os.path.exists(path):
            dir_path = os.path.dirname(path)
            file_name = os.path.basename(path)
            try:
                all_file_list = os.listdir(dir_path)
            except Exception as e:
                try:
                    bflb_utils.printf(e)
                    return False
                finally:
                    e = None
                    del e

            result = []
            if '*' in file_name:
                file_name = file_name.replace('.', '\\.').replace('*', '.*[一-龥]*')
            for one_name in all_file_list:
                pattern = re.compile(file_name)
                temp_list = pattern.findall(one_name)
                if one_name in temp_list:
                    result += temp_list

            if len(result) > 1:
                bflb_utils.printf('Error: ' + name + ' multiple files were matched! ')
                return False
            if len(result) == 0:
                error = 'Error: ' + name + ':' + path + ' image file is not existed'
                bflb_utils.printf(error)
                return False
            self.config[name]['path'] = os.path.join(dir_path, result[0])
        if addr.find('@partition') != -1:
            bflb_utils.printf('{0} get address from partiton file {1}'.format(name, self.config['partition']['path']))
            success, addr_pt = self.get_addr_from_partition_by_name(name, self.config['partition']['path'], 0)
            if not success:
                bflb_utils.printf('Fail, not find ', name, ' in partition')
                return False
            self.config[name]['addr'] = addr_pt
            bflb_utils.printf('Address=', addr_pt)
            addr = addr_pt
        if not hex_to_dec(addr):
            error = 'Error: ' + addr + ' is invalid hexadecimal value'
            bflb_utils.printf(error)
            return False
        return True

    @staticmethod
    def _print_probe(result):
        print('chip: ' + result.chip)
        print('backend: ' + result.backend)
        print('boot_info: ' + result.boot_info)
        print('mac_address: ' + result.mac_address)
        print('jedec_id: ' + result.jedec_id)
        print('jedec_response: ' + result.jedec_response)
        for line in result.log.splitlines():
            if 'set flash cfg' in line or 'chiptype:' in line:
                print(line.strip())

    def probe_or_read(self, parser, args):
        from blflashcommand import FlashError, probe_uart, read_flash_uart

        if args.interface.lower() != 'uart':
            parser.error('--probe and --read require --interface uart')
        if not args.port:
            parser.error('--port is required for --probe and --read')
        try:
            if args.read:
                address_text, separator, length_text = args.read.partition(':')
                if not separator:
                    parser.error('--read must be ADDR:LENGTH, for example 0x0:16')
                try:
                    address = int(address_text, 0)
                    length = int(length_text, 0)
                except ValueError:
                    parser.error('--read ADDR and LENGTH must be integers')
                result = read_flash_uart(
                    address, length, chip=args.chipname, port=args.port,
                    baudrate=args.baudrate, include_probe=args.probe,
                )
                if result.probe is not None:
                    self._print_probe(result.probe)
                print('address: 0x%X' % result.address)
                print('length: %d' % len(result.data))
                print(result.data.hex(' '))
            else:
                self._print_probe(probe_uart(
                    chip=args.chipname, port=args.port, baudrate=args.baudrate,
                ))
        except (FlashError, ValueError) as error:
            print('Error: ' + str(error), file=sys.stderr)
            log = getattr(error, 'log', '')
            if log:
                print(log, file=sys.stderr)
            sys.exit(1)

    def main(self, argv):
        port = None
        ports = []
        for item in get_serial_ports():
            ports.append(item['port'])

        if ports:
            try:
                port = sorted(ports, key=(lambda x: int(re.match('COM(\\d+)', x).group(1))
))[0]
            except Exception:
                port = sorted(ports)[0]

        parser = argparse.ArgumentParser(description='flash-command')
        parser.add_argument('--interface', dest='interface', default='uart', help='interface to use')
        parser.add_argument('--port', dest='port', default=port, help='serial port to use')
        parser.add_argument('--chipname', dest='chipname', default='BL602', help='chip name: BL602, BL702, BL702L, BL808, BL606P, BL616 or BL618 (case-insensitive)')
        parser.add_argument('--baudrate', dest='baudrate', default=2000000, type=int, help='the speed at which to communicate')
        parser.add_argument('--config', dest='config', default='', help='flash config with filedir/address sections (required)')
        parser.add_argument('--cpu_id', dest='cpu_id', default='', help='cpu id')
        parser.add_argument('--efuse', dest='efuse', default='', help='efuse options')
        parser.add_argument('--key', dest='key', default='', help='aes key')
        parser.add_argument('--iv', dest='iv', default='', help='aes iv')
        parser.add_argument('--pk', dest='pk', help='ecc public key')
        parser.add_argument('--sk', dest='sk', default='', help='ecc private key')
        parser.add_argument('--probe', action='store_true', help='read identity/flash evidence without writing')
        parser.add_argument('--read', metavar='ADDR:LENGTH', help='read flash bytes and print them as hex')
        args = parser.parse_args(argv)
        if args.probe or args.read:
            self.probe_or_read(parser, args)
            return 0
        if args.port:
            bflb_utils.printf('Serial port is ' + args.port)
        else:
            if port:
                bflb_utils.printf('Serial port is ' + port)
            else:
                bflb_utils.printf('Serial port is not found')
        bflb_utils.printf('==================================================')
        config = self.get_value(args)
        if config:
            self.obj = InterfaceEflashLoader()
            result = self.obj.run('download', config, None)
            if result is not True:
                bflb_utils.printf('Flash failed: ' + str(result))
                sys.exit(1)
        else:
            sys.exit(2)


if __name__ == '__main__':
    sys.exit(MainClass().main(sys.argv[1:]))
# okay decompiling BLFlashCommand.pyc
