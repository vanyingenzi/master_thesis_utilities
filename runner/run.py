from src.args_parser import Arguments
from src.perfomance_runner import PerfomanceRunner
from src.models import TestbedConfig, YamlConfig
from pprint import pprint

def main():
    args = Arguments.parse_argument()
    runner = PerfomanceRunner(
        testbed=TestbedConfig.parse_json(args.testbed_json),
        config=YamlConfig.parse_yaml(args.config_yaml), 
        debug=args.debug
    )
    runner.run()
    
if __name__ == "__main__":
    main()