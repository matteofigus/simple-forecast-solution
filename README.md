# Amazon Simple Forecast Solution

![](https://img.shields.io/badge/license-MIT--0-green)
![](https://img.shields.io/github/workflow/status/aws-samples/simple-forecast-solution/pytest/main)

## Installation

### _Prerequisite_ – Install `npm` and `aws-cdk`

```bash
# Install npm
curl -L https://git.io/n-install | bash
source ~/.bashrc

# Install aws-cdk
npm i -g aws-cdk
```

### _Prerequisite_ – Install `lambdamap`

```bash
# Clone the lambdamap repository
git clone https://github.com/aws-samples/lambdamap.git

# Install the lambdamap Python library
cd ./lambdamap
pip3 install -e .

# Deploy the lambdamap cloudformation stack
cd ./cdk
cdk bootstrap
cdk deploy
```

### Install SFS

```bash
# Clone the SFS git repository
git clone https://github.com/aws-samples/simple-forecast-solution.git

# Install the SFS library
cd simple-forecast-solution
pip3 install -e .
```

## Run the app

```bash
streamlit ...
```
